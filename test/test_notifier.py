import asyncio
import os
import tempfile
import unittest
from unittest import mock

from db import Database
from notifier import Notifier


class NotifierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-nf-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"notify": {"default_level": "warn"}}
        self.n = Notifier(self.db, self.cfg)

    def tearDown(self):
        self.db.close()

    def event(self, **kw):
        base = {"type": "failed", "session_id": "s1", "name": "n1",
                "tool": "claude", "project": "/p", "state": "stopped",
                "exit_code": 1, "signal": None, "ts": 1.0}
        base.update(kw)
        return base

    async def test_no_rules_still_records_warn(self):
        self.n.handle_event(self.event())
        await asyncio.sleep(0.1)
        page = await self.db.list_notifications(1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["event_type"], "failed")

    async def test_dedup_window(self):
        self.n.handle_event(self.event(ts=1000.0))
        self.n.handle_event(self.event(ts=1000.5))
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.list_notifications(1))["total"], 1)

    async def test_removed_event_not_recorded(self):
        """removed(删标签页)事件不落库 — 防删标签页即发垃圾通知。"""
        self.n.handle_event(self.event(type="removed", state="removed"))
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.list_notifications(1))["total"], 0)

    async def test_rule_suppressed_by_quiet_hours(self):
        await self.db.upsert_rule({
            "name": "q", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "warn",
            "quiet_start": "00:00", "quiet_end": "23:59", "enabled": 1})
        self.n.handle_event(self.event())
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.list_notifications(1))["total"], 0)

    async def test_matched_rules_recorded(self):
        # N2: the notification records which rules matched.
        await self.db.upsert_rule({
            "name": "audit-me", "event_type": "failed",
            "matcher_json": '{"tool": "claude"}', "action": "email",
            "level": "warn", "quiet_start": "", "quiet_end": "",
            "enabled": 1})
        self.n.handle_event(self.event())
        await asyncio.sleep(0.1)
        items = (await self.db.list_notifications(1))["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("audit-me", items[0]["matched_rules"] or "")

    async def test_rule_level_escalation_and_mail(self):
        await self.db.upsert_rule({
            "name": "r", "event_type": "failed",
            "matcher_json": '{"tool": "claude"}', "action": "email",
            "level": "critical", "quiet_start": "", "quiet_end": "",
            "enabled": 1})
        with mock.patch.object(self.n, "_send_mail", return_value=None) as send:
            self.n.handle_event(self.event())
            await asyncio.sleep(0.1)
            self.assertTrue(send.called)
        page = await self.db.list_notifications(1)
        self.assertEqual(page["items"][0]["level"], "critical")
        # 已发送 → delivered=1
        self.assertEqual(page["items"][0]["delivered"], 1)

    async def test_send_pending_retries_undelivered(self):
        nid = await self.db.add_notification({
            "event_type": "failed", "level": "warn", "tool": "t",
            "project": "/p", "session_id": "s9", "title": "x", "body": "b",
            "dedup_key": "k9"})
        with mock.patch.object(self.n, "_send_mail", return_value=None) as send:
            sent = await self.n.send_pending()
        self.assertEqual(sent, 1)
        rows = await self.db.query("SELECT delivered FROM notifications WHERE id=?", (nid,))
        self.assertEqual(rows[0]["delivered"], 1)

    async def test_send_mail_runs_in_executor(self):
        """_send_mail 的 SMTP 调用包在 run_in_executor(不阻塞事件循环)。"""
        import asyncio as _a
        calls = []
        orig_send = self.n.mailer.send

        def spy(subject, html):
            calls.append((subject, html))
            return None
        # mailer 默认未启用(cfg 无 smtp)会提前 return,先启用以到达 send 调用
        self.n.mailer.host = "smtp.example"
        self.n.mailer.send = spy
        # 直接调 _send_mail(已落库一条通知)
        nid = await self.db.add_notification({
            "event_type": "failed", "level": "warn", "tool": "t",
            "project": "/p", "session_id": "s-x", "title": "t",
            "body": "b", "dedup_key": "k-x"})
        await self.n._send_mail(nid, {"type": "failed", "name": "n",
                                      "tool": "t", "project": "/p",
                                      "exit_code": 1})
        self.assertEqual(len(calls), 1)
        self.n.mailer.send = orig_send


if __name__ == "__main__":
    unittest.main()
