import asyncio
import hashlib
import json
import os
import tarfile
import tempfile
import unittest

from backup import (collect_state, create_backup_async, diff_backups,
                    list_backups, restore_backup, rotate)
from db import Database


class BackupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-bak-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "backups"))
        cfg_path = os.path.join(self.data, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"port": 4790, "sessions": [{"id": "s1", "name": "n1"}]}, f)
        self.db = Database(os.path.join(self.data, "webpty.db"))
        self.db.connect()
        self.config = {"port": 4790, "sessions": [{"id": "s1", "name": "n1"}],
                       "backup": {"retention": 2}}

    def tearDown(self):
        self.db.close()

    async def test_create_backup_makes_tar_with_manifest(self):
        b = await create_backup_async(self.data, self.config, self.db)
        self.assertTrue(b["sha256"])
        path = os.path.join(self.data, "backups", b["filename"])
        self.assertTrue(os.path.exists(path))
        with tarfile.open(path) as tf:
            names = tf.getnames()
        self.assertIn("manifest.json", names)
        with open(path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), b["sha256"])
        self.assertEqual(len(await list_backups(self.db)), 1)

    async def test_restore_roundtrip(self):
        b = await create_backup_async(self.data, self.config, self.db)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"port": 9999}, f)  # 破坏配置
        res = await restore_backup(b["id"], self.data, self.db, self.config)
        self.assertTrue(res["ok"])
        with open(os.path.join(self.data, "config.json")) as f:
            restored = json.load(f)
        self.assertEqual(restored["port"], 4790)
        # merge 保留现有 sessions
        self.assertIn("sessions", restored)

    async def test_rotate_keeps_retention(self):
        made = []
        for _ in range(4):
            b = await create_backup_async(self.data, self.config, self.db)
            made.append(b)
        deleted = await rotate(self.db, 2)
        self.assertEqual(len(deleted), 2)
        remaining = await list_backups(self.db)
        self.assertEqual(len(remaining), 2)
        deleted_ids = set(deleted)
        for b in made:
            path = os.path.join(self.data, "backups", b["filename"])
            if b["id"] in deleted_ids:
                self.assertFalse(os.path.exists(path))
            else:
                self.assertTrue(os.path.exists(path))

    async def test_diff_backups(self):
        a = await create_backup_async(self.data, self.config, self.db)
        self.config["port"] = 4800
        b = await create_backup_async(self.data, self.config, self.db)
        diff = await diff_backups(a["id"], b["id"], self.db)
        self.assertTrue(any(d["key"] == "port" for d in diff))


if __name__ == "__main__":
    unittest.main()
