import asyncio
import json
import os
import tempfile
import unittest

from cost_tracker import CostTracker
from db import Database


class CostTrackerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-cost-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"prices": {"claude": {"input": 10.0, "output": 20.0,
                                          "cache_hit": 1.0, "currency": "USD"}}}
        self.c = CostTracker(self.db, self.cfg)

    def tearDown(self):
        self.db.close()

    def ev(self, line, sid="s1", tool="claude"):
        return {"type": "result", "raw": line, "session_id": sid, "tool": tool}

    async def test_records_from_raw_stream_json(self):
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 500}})
        self.c.handle_agent_event(self.ev(line))
        await asyncio.sleep(0.1)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 500)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 500*20/1e6

    async def test_records_from_embedded_usage(self):
        self.c.handle_agent_event({"type": "result", "session_id": "s2",
                                   "tool": "claude",
                                   "usage": {"input_tokens": 1000,
                                             "output_tokens": 0}})
        await asyncio.sleep(0.1)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 1000)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 1000*10/1e6

    async def test_ignores_unparseable(self):
        self.c.handle_agent_event(self.ev("garbage"))
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 0)

    async def test_budget_alerts(self):
        await self.c.set_budget(0.001)
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 1000}})
        self.c.handle_agent_event(self.ev(line))
        await asyncio.sleep(0.1)
        self.assertTrue(await self.c.over_budget())

    async def test_summary_and_grouped(self):
        for i, tool in enumerate(("claude", "codex")):
            self.c.handle_agent_event(self.ev(
                json.dumps({"type": "message_delta",
                            "usage": {"output_tokens": 100}}),
                sid=f"s{i}", tool=tool))
        await asyncio.sleep(0.1)
        g = await self.c.grouped("tool", "day")
        self.assertEqual(len(g), 2)


if __name__ == "__main__":
    unittest.main()
