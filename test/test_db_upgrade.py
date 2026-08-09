"""Audit L6: legacy-DB → new-version upgrade smoke tests.

Builds a database with the OLD schema (pre-versioning), then connects with
the current Database class and verifies migrations run and every INSERT
path works (columns added by migration exist).
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from db import Database  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'warn',
    tool TEXT, project TEXT, session_id TEXT,
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT, project TEXT, tool TEXT, model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'realtime'
);
CREATE TABLE backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    created_at REAL NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    encrypted INTEGER NOT NULL DEFAULT 0,
    retained INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    created_at REAL NOT NULL,
    source_node TEXT,
    mode TEXT NOT NULL DEFAULT 'merge',
    status TEXT NOT NULL DEFAULT 'pending',
    log TEXT NOT NULL DEFAULT ''
);
"""


class DbUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-dbup-")
        self.path = os.path.join(self.tmp, "webpty.db")
        # Build a legacy DB (old schema, user_version = 0).
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_db_upgrades_and_inserts(self):
        db = Database(self.path)
        db.connect()

        async def _smoke():
            # Notifications: new columns (matched_rules/attempts/last_error)
            # must exist and accept inserts.
            await db.add_notification({
                "ts": 1.0, "event_type": "completed", "level": "warn",
                "tool": "bash", "project": "/p", "session_id": "s1",
                "title": "t", "body": "b", "dedup_key": "k1"})
            # token_usage insert (all columns)
            await db.add_usage({
                "project": "/p", "tool": "bash", "model": "m",
                "session_id": "s2", "tokens_in": 1, "tokens_out": 2,
                "cost": 0.5, "source": "realtime"})
            # backups insert
            await db.add_backup({
                "filename": "x.tar.gz", "size_bytes": 1, "sha256": "aa",
                "manifest_json": "{}", "encrypted": 0, "retained": 1})
            # queries that touch the migrated columns
            rows = await db.list_rules()
            self.assertIsInstance(rows, list)
            rows = await db.list_backups()
            self.assertEqual(len(rows), 1)
            s = await db.usage_summary("month")
            # realtime rows land in estimated, not cost (cost = actual only)
            self.assertAlmostEqual(s["estimated"], 0.5, places=6)

        asyncio.run(_smoke())
        # user_version should now be >= 1.
        conn = sqlite3.connect(self.path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(ver, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
