import asyncio
import os
import tempfile
import unittest

from db import Database


class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-db-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()

    def tearDown(self):
        self.db.close()

    async def test_schema_creates_all_tables(self):
        rows = await self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = [r["name"] for r in rows]
        for t in ("notifications", "notification_rules", "token_usage",
                  "backups", "migrations"):
            self.assertIn(t, names)

    async def test_wal_mode_enabled(self):
        row = await self.db.query_one("PRAGMA journal_mode")
        self.assertEqual(row["journal_mode"], "wal")

    async def test_notification_crud_and_dedup(self):
        nid = await self.db.add_notification({
            "event_type": "completed", "level": "info", "tool": "claude",
            "project": "/p", "session_id": "s1", "title": "t",
            "body": "b", "dedup_key": "s1|completed|info"})
        self.assertGreater(nid, 0)
        self.assertTrue(await self.db.dedup_recent("s1|completed|info", 60))
        self.assertFalse(await self.db.dedup_recent("s1|completed|warn", 60))
        self.assertFalse(await self.db.dedup_recent("s1|completed|info", 0))
        page = await self.db.list_notifications(1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["session_id"], "s1")
        await self.db.mark_delivered(nid, True)
        self.assertEqual(len(await self.db.pending_notifications()), 0)

    async def test_rules_upsert_delete(self):
        rid = await self.db.upsert_rule({
            "name": "r1", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "critical",
            "quiet_start": "22:00", "quiet_end": "08:00", "enabled": 1})
        rules = await self.db.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "r1")
        await self.db.upsert_rule({
            "id": rid, "name": "r1b", "event_type": "failed",
            "matcher_json": "{}", "action": "email", "level": "critical",
            "quiet_start": "", "quiet_end": "", "enabled": 1})
        self.assertEqual(len(await self.db.list_rules()), 1)
        await self.db.delete_rule(rid)
        self.assertEqual(len(await self.db.list_rules()), 0)

    async def test_usage_summary_and_grouped(self):
        for i in range(3):
            await self.db.add_usage({
                "project": f"/p{i}", "tool": "claude", "model": "claude-4",
                "session_id": f"s{i}", "tokens_in": 1000, "tokens_out": 500,
                "cost": 0.05, "source": "realtime"})
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 3000)
        self.assertEqual(s["tokens_out"], 1500)
        # 无 actual 行时 cost=0,估算值在 estimated
        self.assertAlmostEqual(s["cost"], 0.0, places=6)
        self.assertAlmostEqual(s["estimated"], 0.15, places=6)
        grouped = await self.db.usage_grouped("project", "day")
        self.assertEqual(len(grouped), 3)
        # 无 actual 行时 grouped 的 cost=0(估算不计入)
        self.assertAlmostEqual(sum(g["cost"] for g in grouped), 0.0, places=6)

    async def test_backup_and_migration_tables(self):
        bid = await self.db.add_backup({
            "filename": "webpty-1.tar.gz", "size_bytes": 10, "sha256": "x",
            "manifest_json": "{}", "encrypted": 0, "retained": 1})
        self.assertIsNotNone(await self.db.get_backup(bid))
        self.assertEqual(len(await self.db.list_backups()), 1)
        await self.db.delete_backup(bid)
        self.assertIsNone(await self.db.get_backup(bid))
        mid = await self.db.add_migration({
            "filename": "webpty-migrate-1.tar.gz", "source_node": "node-a",
            "mode": "merge", "status": "done", "log": "ok"})
        self.assertEqual(len(await self.db.list_migrations()), 1)
        self.assertGreater(mid, 0)


if __name__ == "__main__":
    unittest.main()
