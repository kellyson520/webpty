"""Task-status notifications: session_event → rule match → SQLite + SMTP.

Business-management layer. Subscribes to session_manager's `session_event`
bus events; dedups by (session_id, event_type, level) within 60s.
"""
from __future__ import annotations

import asyncio
import time
from html import escape
from typing import Any

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
        task.add_done_callback(self._tasks.discard)

    async def _process(self, event: dict) -> None:
        # removed(删标签页)不产生通知:matched 为空只影响 level/邮件,
        # 落库仍会发生 → 需显式过滤,否则 send_pending 会补发邮件。
        if event.get("type") == "removed":
            return
        rules = await self.db.list_rules()
        matched = [r for r in rules if r.get("enabled")
                   and match_rule(r, event)]
        level = event.get("level") or self.config.get("notify", {}).get(
            "default_level", "warn")
        if matched:
            level = max(matched, key=lambda r: {"info": 0, "warn": 1,
                                                "critical": 2}.get(
                r.get("level", "warn"), 1)).get("level", level)
        dedup_key = f"{event.get('session_id')}|{event.get('type')}|{level}"
        if await self.db.dedup_recent(dedup_key, DEDUP_WINDOW_S):
            return
        had_rules = bool(matched)
        for rule in matched:
            # Audit 3.3: quiet hours used to drop the WHOLE event when any
            # matched rule was quiet — even email actions from other rules.
            # Only skip the email send for quiet rules; the notification
            # still lands in the center.
            if quiet_hours_active(rule) and rule.get("action") == "email":
                matched = [r for r in matched if r is not rule]
        # All matched rules were email-only AND quiet → skip entirely.
        # (No rules at all still records the warn notification below.)
        if had_rules and not matched:
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
                     f"{event.get('type', 'event')}",
            "body": f"会话 {event.get('name')} 已 {event.get('type')}",
            "dedup_key": dedup_key,
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
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self.mailer.send,
            f"[webpty] {event.get('tool')} {event.get('type')}",
            HTML_TMPL.format(
                title=escape(str(event.get("name", "session"))),
                body=escape(str(event.get("type", ""))),
                meta=escape(f"project: {event.get('project')}\n"
                            f"exit_code: {event.get('exit_code')}"),
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
            except Exception:  # noqa: BLE001
                continue
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
                self.mailer.send(subject="[webpty] test notification",
                                 html="<p>test</p>")
                return True
            except Exception:  # noqa: BLE001
                return False

        return await loop.run_in_executor(None, _send)
