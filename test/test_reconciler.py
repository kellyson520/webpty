import asyncio
import json
import os
import tempfile
import unittest

from db import Database
from reconciler import Reconciler, scan_claude_logs


class ReconcilerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-rec-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"prices": {"claude": {"input": 10.0, "output": 20.0,
                                          "cache_hit": 1.0, "currency": "USD"}}}
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.projects, "proj-a"))
        with open(os.path.join(self.projects, "proj-a", "session-x.jsonl"), "w") as f:
            f.write(json.dumps({"type": "message_delta",
                                "usage": {"output_tokens": 100}}) + "\n")
            f.write("garbage line\n")
            f.write(json.dumps({"type": "message_delta",
                                "usage": {"output_tokens": 50}}) + "\n")

    def tearDown(self):
        self.db.close()

    def test_scan_claude_logs(self):
        items = scan_claude_logs(self.projects)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["tokens_out"] > 0 for i in items))

    async def test_reconcile_persists_posthoc(self):
        r = Reconciler(self.db, self.cfg)
        added = await r.reconcile(self.projects)
        self.assertEqual(added, 2)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 150)
        self.assertAlmostEqual(s["cost"], 0.003, places=6)  # 150*20/1e6

    async def test_reconcile_idempotent(self):
        r = Reconciler(self.db, self.cfg)
        await r.reconcile(self.projects)
        added2 = await r.reconcile(self.projects)
        self.assertEqual(added2, 0)
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 2)


if __name__ == "__main__":
    unittest.main()
