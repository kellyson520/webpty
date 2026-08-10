"""Task-status notifications: session_event → rule match → SQLite + SMTP.

Business-management layer. Subscribes to session_manager's `session_event`
bus events; dedups by (session_id, event_type, level) within 60s.
"""
from __future__ import annotations

import asyncio
import time
from html import escape
from typing import Any

# Audit L3 (v23): user-facing event names in Chinese (notification-center
# titles/bodies AND email subject/body were English).
EVENT_ZH = {
    "completed": "已完成",
    "failed": "失败",
    "crashed": "崩溃",
    "terminated": "已停止",
    "stopped": "已停止",
    "removed": "已删除",
    "disk_low": "磁盘空间不足",
    "budget_over": "预算超限",
    "budget_ok": "预算恢复",
}

from mailer import Mailer
from rules import match_rule, quiet_hours_active

DEDUP_WINDOW_S = 60.0
HTML_TMPL = """<div style="font-family:sans-serif">
<h3>{title}</h3><p>{body}</p>
<pre style="background:#f5f5f5;padding:8px">{meta}</pre></div>"""


class Notifier:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config
        self.mailer = Mailer(config)
        self._tasks: set[asyncio.Task] = set()

    def handle_event(self, event: dict) -> None:
        """Sync callback for the event bus; spawns an async task."""
        task = asyncio.create_task(self._process(event))
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        """Audit M4: surface background failures instead of letting them
        become unretrieved Task exceptions."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        err = task.exception()
        if err is not None:
            from logging_util import log_error
            log_error("notifier", err)

    async def _process(self, event: dict) -> None:
        # removed(删标签页)不产生通知:matched 为空只影响 level/邮件,
        # 落库仍会发生 → 需显式过滤,否则 send_pending 会补发邮件。
        if event.get("type") == "removed":
            return
        rules = await self.db.list_rules()
        matched = [r for r in rules if r.get("enabled")
                   and match_rule(r, event)]
        # Audit M3 (v28): quiet email rules must not inflate the level —
        # filter them FIRST, then compute level over what will actually
        # fire (a quiet email rule used to lift the stored level to
        # critical even though it never sent anything).
        quieted = [r for r in matched
                   if quiet_hours_active(r) and r.get("action") == "email"]
        matched = [r for r in matched if r not in quieted]
        level = event.get("level") or self.config.get("notify", {}).get(
            "default_level", "warn")
        if matched:
            level = max(matched, key=lambda r: {"info": 0, "warn": 1,
                                                "critical": 2}.get(
                r.get("level", "warn"), 1)).get("level", level)
        dedup_key = f"{event.get('session_id')}|{event.get('type')}|{level}"
        if await self.db.dedup_recent(dedup_key, DEDUP_WINDOW_S):
            return
        # All matched rules were email-only AND quiet → skip entirely.
        # (No rules at all still records the warn notification below.)
        if quieted and not matched:
            return
        nid = await self.db.add_notification({
            "event_type": event.get("type", "unknown"),
            "level": level,
            "tool": event.get("tool"),
            "project": event.get("project"),
            # Audit N2: record which rules matched (for the UI audit trail).
            "matched_rules": ",".join(sorted(
                {r.get("name") or r.get("event_type") or "?" for r in matched})),
            "session_id": event.get("session_id"),
            "title": f"{event.get('tool', 'session')} "
                     f"{EVENT_ZH.get(event.get('type', ''), event.get('type', 'event'))}",
            "body": f"会话 {event.get('name')} "
                    f"{EVENT_ZH.get(event.get('type', ''), event.get('type', 'event'))}",
            "dedup_key": dedup_key,
            # Audit M3 (v28): quieted email rules — record for the center,
            # but never let send_pending mail it later (unless another
            # non-quiet email rule is sending right now, in which case the
            # retry path must stay open).
            "suppress_email": bool(quieted) and not any(
                r.get("action") == "email" for r in matched),
        })
        wants_mail = any(r.get("action") == "email" for r in matched)
        if wants_mail:
            try:
                await self._send_mail(nid, event)
                await self.db.mark_delivered(nid, True)
            except Exception:  # noqa: BLE001 — mail failure must not break flow
                return  # keep delivered=0 → retried by send_pending

    async def _send_mail(self, nid: int, event: dict) -> None:
        if not self.mailer.enabled():
            await self.db.mark_delivered(nid, True)
            return
        zh = EVENT_ZH.get(event.get("type", ""), event.get("type", ""))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self.mailer.send,
            f"[webpty] {event.get('tool')} {zh}",
            HTML_TMPL.format(
                title=escape(str(event.get("name", "session"))),
                body=escape(zh),
                meta=escape(f"项目: {event.get('project')}\n"
                            f"退出码: {event.get('exit_code')}"),
            ))
        await self.db.mark_delivered(nid, True)

    async def send_pending(self) -> int:
        sent = 0
        for row in await self.db.pending_notifications():
            event = dict(row)
            event.setdefault("type", row.get("event_type"))
            event.setdefault("name", row.get("title"))
            try:
                await self._send_mail(row["id"], event)
                await self.db.mark_delivered(row["id"], True)
                sent += 1
            except Exception as err:  # noqa: BLE001
                # Audit T4: record the failure; give up after MAX_ATTEMPTS
                # (delivered=2 = dead) instead of retrying forever until
                # prune_old_data deletes the row.
                await self.db.bump_notify_attempt(row["id"], str(err)[:300])
        return sent

    async def test_message(self) -> bool:
        if not self.mailer.enabled():
            return False
        # Audit F2: smtplib send blocks up to 15s (mailer timeout); running
        # it inline froze the whole event loop on "测试邮件". Same
        # executor path as _send_mail.
        loop = asyncio.get_event_loop()

        def _send() -> bool:
            try:
                self.mailer.send(subject="[webpty] 测试通知",
                                 html="<p>这是一封测试邮件，说明 SMTP 配置正确。</p>")
                return True
            except Exception:  # noqa: BLE001
                return False

        return await loop.run_in_executor(None, _send)
