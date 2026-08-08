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
    async def test_realtime_cumulative_dedup(self):
        """同一会话的累计 usage 事件只按增量落库(不重复计费)。"""
        # 三次累计事件:100/50 → 150/80 → 150/80(末次重复)
        for u in ({"prompt_tokens": 100, "completion_tokens": 50},
                  {"prompt_tokens": 150, "completion_tokens": 80},
                  {"prompt_tokens": 150, "completion_tokens": 80}):
            await self.c._record({
                "usage": u, "tool": "codex", "project": "/p",
                "session_id": "sid-acc"}, "sid-acc")
        rows = await self.db.query(
            "SELECT tokens_in, tokens_out FROM token_usage "
            "WHERE session_id='sid-acc' ORDER BY id")
        self.assertEqual(len(rows), 2, "应只落 2 行(全量+增量), 重复值跳过")
        self.assertEqual(rows[0]["tokens_in"], 100)
        self.assertEqual(rows[1]["tokens_in"], 50)  # 增量
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 150)  # = 最终累计值,不重复
        self.assertEqual(s["tokens_out"], 80)

    async def test_session_end_clears_cumulative(self):
        """会话结束清除累计状态,新会话首条按全量记。"""
        await self.c._record({
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "tool": "codex", "project": "/p", "session_id": "sid-clr"}, "sid-clr")
        self.c.on_session_event({"type": "completed", "session_id": "sid-clr"})
        await self.c._record({
            "usage": {"prompt_tokens": 200, "completion_tokens": 100},
            "tool": "codex", "project": "/p", "session_id": "sid-clr"}, "sid-clr")
        rows = await self.db.query(
            "SELECT tokens_in FROM token_usage WHERE session_id='sid-clr' ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["tokens_in"], 200)  # 全量,非增量


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

    async def test_embedded_usage_records_delta_cost(self):
        """usage dict 事件走 parse_usage 归一化,成本按 delta 重算。"""
        self.c.handle_agent_event({"type": "result", "session_id": "s3",
                                   "tool": "claude",
                                   "usage": {"input_tokens": 1000,
                                             "output_tokens": 0}})
        await self._settle()
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 1000)
        # 1000 in * 10(测试价)/1e6 = 0.01
        self.assertAlmostEqual(s["cost"], 0.01, places=6)


if __name__ == "__main__":
    unittest.main()
