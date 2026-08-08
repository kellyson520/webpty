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

    async def _settle(self):
        """Wait for in-flight _record tasks instead of fixed sleeps."""
        if self.c._tasks:
            await asyncio.gather(*self.c._tasks)
        else:
            await asyncio.sleep(0.05)

    async def test_records_from_raw_stream_json(self):
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 500}})
        self.c.handle_agent_event(self.ev(line))
        await self._settle()
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 500)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 500*20/1e6

    async def test_records_from_embedded_usage(self):
        self.c.handle_agent_event({"type": "result", "session_id": "s2",
                                   "tool": "claude",
                                   "usage": {"input_tokens": 1000,
                                             "output_tokens": 0}})
        await self._settle()
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 1000)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 1000*10/1e6

    async def test_ignores_unparseable(self):
        self.c.handle_agent_event(self.ev("garbage"))
        await self._settle()
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 0)

    async def test_budget_alerts(self):
        await self.c.set_budget(0.001)
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 1000}})
        self.c.handle_agent_event(self.ev(line))
        await self._settle()
        self.assertTrue(await self.c.over_budget())

    async def test_summary_and_grouped(self):
        for i, tool in enumerate(("claude", "codex")):
            self.c.handle_agent_event(self.ev(
                json.dumps({"type": "message_delta",
                            "usage": {"output_tokens": 100}}),
                sid=f"s{i}", tool=tool))
        await self._settle()
        g = await self.c.grouped("tool", "day")
        self.assertEqual(len(g), 2)

    async def test_records_from_usage_agent_event(self):
        """SessionManager re-emits usage-bearing stream lines as
        {"type": "usage", "raw": ..., "tool": ...} — CostTracker must
        consume that bus shape."""
        self.c.handle_agent_event({
            "type": "usage",
            "raw": json.dumps({"type": "message_delta",
                                "usage": {"output_tokens": 100}}),
            "tool": "claude"})
        await self._settle()
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 100)

    async def test_embedded_usage_cost_wins(self):
        """Event-supplied cost is used verbatim, not recomputed via
        price_table."""
        self.c.handle_agent_event({"type": "result", "session_id": "s3",
                                   "tool": "claude",
                                   "usage": {"input_tokens": 1000,
                                             "output_tokens": 0,
                                             "cost": 0.5}})
        await self._settle()
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 1000)
        self.assertAlmostEqual(s["cost"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main()

    async def test_realtime_skips_posthoc_duplicate(self):
        # 先写入 posthoc 行（模拟 reconciler 已记录）→ realtime 同对跳过
        await self.db.add_usage({
            "project": "/p", "tool": "claude", "model": "claude-haiku",
            "session_id": "s-dup", "tokens_in": 100, "tokens_out": 50,
            "cost": 0.01, "source": "posthoc"})
        self.c.handle_agent_event(self.ev(
            json.dumps({"type": "message_delta",
                        "usage": {"output_tokens": 50}}),
            sid="s-dup", tool="claude"))
        await asyncio.sleep(0.1)
        rows = await self.db.query("SELECT source FROM token_usage WHERE session_id='s-dup'")
        # 只有 posthoc 一条（realtime 被去重跳过）
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "posthoc")
