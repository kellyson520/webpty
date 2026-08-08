"""Task-status notifications: session_event → rule match → SQLite + SMTP.

Business-management layer. Subscribes to session_manager's `session_event`
bus events; dedups by (session_id, event_type, level) within 60s.
"""
from __future__ import annotations

import asyncio
import time
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
        for rule in matched:
            if quiet_hours_active(rule):
                return
        nid = await self.db.add_notification({
            "event_type": event.get("type", "unknown"),
            "level": level,
            "tool": event.get("tool"),
            "project": event.get("project"),
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
        self.mailer.send(
            subject=f"[webpty] {event.get('tool')} {event.get('type')}",
            html=HTML_TMPL.format(
                title=event.get("name", "session"),
                body=event.get("type", ""),
                meta=f"project: {event.get('project')}\n"
                     f"exit_code: {event.get('exit_code')}",
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
        try:
            self.mailer.send(subject="[webpty] test notification",
                             html="<p>test</p>")
            return True
        except Exception:  # noqa: BLE001
            return False
