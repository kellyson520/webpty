import asyncio
import json
import os
import tempfile
import unittest

from db import Database
from migrator import Migrator, WorkerInterface


class MigratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-mig-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"port": 4790, "roots": ["/a"]}, f)
        self.db = Database(os.path.join(self.data, "webpty.db"))
        self.db.connect()
        self.config = {"port": 4790, "roots": ["/a"]}
        self.m = Migrator(self.data, self.config, self.db)

    def tearDown(self):
        self.db.close()

    async def test_worker_interface_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            WorkerInterface().export_state()

    async def test_export_creates_package(self):
        path = await self.m.export()
        self.assertTrue(os.path.exists(path))
        self.assertIn("webpty-migrate-", os.path.basename(path))
        self.assertGreater(os.path.getsize(path), 0)

    async def test_import_merge_preserves_existing(self):
        path = await self.m.export()
        # 修改现有配置制造冲突
        self.config["port"] = 9999
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["mode"], "merge")
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.config["port"], 4790)  # 包内值覆盖
        self.assertEqual(self.config["extra_key"], "mine")  # 现有键保留

    async def test_import_dry_run_does_not_write(self):
        path = await self.m.export()
        before = dict(self.config)
        res = await self.m.import_package(path, "dry-run")
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(self.config, before)

    async def test_import_replace_overwrites(self):
        path = await self.m.export()
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "replace")
        self.assertNotIn("extra_key", self.config)

    async def test_source_node_id_stable(self):
        a = self.m.source_node_id()
        b = self.m.source_node_id()
        self.assertEqual(a, b)
        self.assertTrue(len(a) > 8)


if __name__ == "__main__":
    unittest.main()
