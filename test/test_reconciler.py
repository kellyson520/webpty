import asyncio
import json
import os
import tempfile
import unittest

from cost_tracker import CostTracker
from db import Database
from reconciler import Reconciler, scan_claude_logs, scan_tool_logs


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
        # session_id 从文件名推导，project 取日志所在目录
        self.assertTrue(all(i["session_id"] == "session-x" for i in items))
        self.assertTrue(all(i["project"] == os.path.join(self.projects, "proj-a")
                            for i in items))

    def test_scan_tool_logs_estimates_reasonix(self):
        """Audit H1 (v22): reasonix session JSONLs have no usage fields —
        tokens are estimated from content length, model from filename."""
        import json as _json
        rx = os.path.join(self.tmp, "reasonix")
        d = os.path.join(rx, "projects", "-root-webpty", "sessions")
        os.makedirs(d)
        with open(os.path.join(d, "20260808-010000-deepseek-v4-flash.jsonl"),
                  "w", encoding="utf-8") as f:
            f.write(_json.dumps({"role": "user", "content": "x" * 40}) + "\n")
            f.write(_json.dumps({"role": "assistant", "content": "y" * 80}) + "\n")
            f.write(_json.dumps({"role": "user",
                                 "content": [{"type": "text", "text": "z" * 16}]}) + "\n")
        items = scan_tool_logs(rx, "reasonix")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tokens_in"], 14)  # (40+16)/4
        self.assertEqual(items[0]["tokens_out"], 20)  # 80/4
        self.assertEqual(items[0]["model"], "deepseek-v4-flash")
        self.assertEqual(items[0]["session_id"], "20260808-010000-deepseek-v4-flash")

    def test_recovery_filename_model_trimmed(self):
        """Audit H3 (v28): reasonix recovery/checkpoint copies embed the
        model mid-name — the garbage tail must be trimmed to the known
        price-table prefix (was parsed whole → 13× family-price overcharge)."""
        import json as _json
        rx = os.path.join(self.tmp, "reasonix")
        d = os.path.join(rx, "projects", "-root-webpty", "sessions")
        os.makedirs(d)
        fn = ("20260807-124828.221742337-deepseek-v4-flash-"
              "6b0ea5910e4b-d98d070d8f51-recovery-f56c7c3d38a2c740.jsonl")
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write(_json.dumps({"role": "user", "content": "x" * 40}) + "\n")
        items = scan_tool_logs(rx, "reasonix")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["model"], "deepseek-v4-flash")

    def test_opencode_dir_is_not_reasonix(self):
        """Audit H2 (v28): opencode scan must NOT walk the reasonix dir."""
        import json as _json
        rx = os.path.join(self.tmp, "reasonix")
        d = os.path.join(rx, "projects", "-root-webpty", "sessions")
        os.makedirs(d)
        with open(os.path.join(d, "20260808-010000-deepseek-v4-flash.jsonl"),
                  "w", encoding="utf-8") as f:
            f.write(_json.dumps({"role": "user", "content": "hi"}) + "\n")
        oc = os.path.join(self.tmp, "opencode")
        self.assertEqual(scan_tool_logs(oc, "opencode"), [])
        self.assertEqual(len(scan_tool_logs(rx, "reasonix")), 1)

    async def test_reconcile_persists_posthoc(self):
        r = Reconciler(self.db, self.cfg)
        added = await r.reconcile(self.projects)
        self.assertEqual(added, 2)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 150)
        self.assertAlmostEqual(s["estimated"], 0.003, places=6)  # 150*20/1e6
        rows = await self.db.query(
            "SELECT source, session_id FROM token_usage")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source"] == "posthoc" for r in rows))
        self.assertTrue(all(r["session_id"] == "session-x" for r in rows))

    async def test_reconcile_idempotent(self):
        r = Reconciler(self.db, self.cfg)
        await r.reconcile(self.projects)
        added2 = await r.reconcile(self.projects)
        self.assertEqual(added2, 0)
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 2)

    async def test_reconcile_no_double_count_after_realtime(self):
        # realtime 已带 sid 记录同一行（100 tokens）后，reconcile 必须跳过它，
        # 只补录 realtime 漏掉的行（50 tokens）——防止双重计数。
        ct = CostTracker(self.db, self.cfg)
        ct.handle_agent_event(
            {"raw": json.dumps({"type": "message_delta",
                                 "usage": {"output_tokens": 100}}),
             "tool": "claude"},
            "session-x")
        while ct._tasks:
            await asyncio.sleep(0.01)
        added = await Reconciler(self.db, self.cfg).reconcile(self.projects)
        self.assertEqual(added, 1)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["entries"], 2)
        self.assertEqual(s["tokens_out"], 150)


    async def test_scan_tail_of_huge_files(self):
        """超大日志文件读取尾部(审计 F1):不再是整体跳过,尾部 usage 仍被扫到。"""
        import os as _os
        from reconciler import MAX_SCAN_FILE_BYTES
        big = _os.path.join(self.projects, "proj-a", "huge.jsonl")
        usage_line = ('{"type":"usage","message":{"usage":{"input_tokens":7,'
                      '"output_tokens":3}}}\n')
        with open(big, "wb") as f:
            # Fill real bytes up to the cap, then the usage line at the
            # tail — the tail-read must parse it.
            chunk = b'{"type":"ignored","message":{}}\n'
            written = 0
            while written + len(chunk) < MAX_SCAN_FILE_BYTES:
                f.write(chunk)
                written += len(chunk)
            f.write(usage_line.encode())
        items = scan_claude_logs(self.projects)
        hits = [i for i in items if i["session_id"] == "huge"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["tokens_in"], 7)


if __name__ == "__main__":
    unittest.main()
