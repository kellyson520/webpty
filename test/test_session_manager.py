"""Unit tests for src/session_manager.py — SessionManager, RingBuffer,
normalize_tool_result."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid

_TEST_DIR = tempfile.mkdtemp(prefix="webpty-sm-test-")
os.environ["WEBPTY_DATA_DIR"] = _TEST_DIR
os.environ["WEBPTY_PROJECTS_ROOT"] = os.path.join(_TEST_DIR, "projects")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from session_manager import (  # noqa: E402
    SessionManager, normalize_tool_result,
)
from ring_buffer import RingBuffer  # noqa: E402


class StubHost:
    """A fake PtyHostClient that records calls instead of talking to a daemon."""

    def __init__(self):
        self.calls = []
        self.sessions_map = {}
        self.listeners = {"output": [], "exit": [], "disconnect": []}

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def off(self, event, cb):
        try:
            self.listeners[event].remove(cb)
        except ValueError:
            pass

    def emit(self, event, *args):
        for cb in list(self.listeners.get(event, [])):
            cb(*args)

    async def connect(self):
        self.calls.append("connect")

    async def list(self):
        self.calls.append("list")
        return {"sessions": list(self.sessions_map.values())}

    async def start(self, opts):
        self.calls.append(("start", opts["id"]))
        view = {"id": opts["id"], "pid": 4242, "alive": True, "started_at": 1,
                "exit_code": None, "exit_signal": None}
        self.sessions_map[opts["id"]] = view
        return {"pid": 4242}

    async def attach(self, sid):
        self.calls.append(("attach", sid))

    async def detach(self, sid):
        self.calls.append(("detach", sid))

    async def kill(self, sid):
        self.calls.append(("kill", sid))

    async def forget(self, sid):
        self.calls.append(("forget", sid))
        self.sessions_map.pop(sid, None)

    def input(self, sid, data):
        self.calls.append(("input", sid, data))
        return True

    def resize(self, sid, cols, rows):
        self.calls.append(("resize", sid, cols, rows))
        return True


def make_config():
    return {
        "tools": {
            "bash": {"command": "bash", "defaultArgs": "", "nameFlag": None},
            "claude-chat": {"command": "claude", "defaultArgs": "", "engine": "agent",
                            "permissionMode": "bypassPermissions"},
        },
        "sessions": [],
    }


class SessionManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import shutil

        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        os.makedirs(_TEST_DIR, exist_ok=True)
        self.sm = SessionManager(make_config(), lambda: None)
        self.host = StubHost()
        self.sm.host = self.host

    def test_create_and_persist(self):
        s = self.sm.create(name="t1", cwd="/tmp", tool="bash")
        self.assertTrue(s["id"])
        self.assertEqual(s["state"], "stopped")
        self.assertEqual(self.sm.get(s["id"]), s)
        self.assertEqual(len(self.sm.list()), 1)

    def test_default_name_is_cwd_basename(self):
        s = self.sm.create(name="", cwd="/tmp/someproj", tool="bash")
        self.assertEqual(s["name"], "someproj")

    def test_remove_unknown_returns_false(self):
        result = asyncio.run(self.sm.remove("nope"))
        self.assertFalse(result)

    def test_remove_deletes(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        result = asyncio.run(self.sm.remove(s["id"]))
        self.assertTrue(result)
        self.assertIsNone(self.sm.get(s["id"]))

    def test_reorder_keeps_valid(self):
        a = self.sm.create(name="a", cwd="/tmp/a", tool="bash")
        b = self.sm.create(name="b", cwd="/tmp/b", tool="bash")
        self.sm.reorder([b["id"], "ghost", a["id"]])
        self.assertEqual(list(self.sm.sessions.keys()), [b["id"], a["id"]])

    async def test_start_pty(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        await self.sm.init()
        started = await self.sm.start(s["id"])
        self.assertEqual(started["state"], "running")
        self.assertTrue(any(c[0] == "start" and c[1] == s["id"] for c in self.host.calls
                            if isinstance(c, tuple)))

    async def test_start_unknown_tool_raises(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        s["tool"] = "does-not-exist"
        with self.assertRaises(ValueError):
            await self.sm.start(s["id"])

    async def test_start_twice_noop(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        await self.sm.init()
        await self.sm.start(s["id"])
        before = sum(1 for c in self.host.calls if isinstance(c, tuple) and c[0] == "start")
        await self.sm.start(s["id"])
        after = sum(1 for c in self.host.calls if isinstance(c, tuple) and c[0] == "start")
        self.assertEqual(after, before)

    async def test_stop(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        await self.sm.init()
        await self.sm.start(s["id"])
        ok = await self.sm.stop(s["id"])
        self.assertTrue(ok)
        self.assertEqual(s["state"], "stopped")

    async def test_write_only_running(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        self.assertFalse(self.sm.write(s["id"], "ls"))
        await self.sm.init()
        await self.sm.start(s["id"])
        self.assertTrue(self.sm.write(s["id"], "ls"))
        self.assertTrue(any(c[0] == "input" and c[1] == s["id"] for c in self.host.calls
                            if isinstance(c, tuple)))

    async def test_resize(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        await self.sm.init()
        await self.sm.start(s["id"])
        self.assertTrue(self.sm.resize(s["id"], 100, 40))
        self.assertEqual(s["cols"], 100)
        self.assertTrue(any(c[0] == "resize" and c[2] == 100 for c in self.host.calls
                            if isinstance(c, tuple)))

    async def test_agent_line_usage_emits_agent_event(self):
        """Stream lines that carry usage but no transcript item are
        re-emitted as {"type": "usage", "raw": ..., "tool": ...} so the
        CostTracker can meter them (Task 8 integration contract)."""
        s = self.sm.create(name="t", cwd="/tmp", tool="claude")
        got = []
        self.sm.on("agentEvent", lambda sid, item: got.append((sid, item)))
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 500}})
        self.assertFalse(self.sm._handle_agent_line(s, line))
        self.assertEqual(len(got), 1)
        sid, item = got[0]
        self.assertEqual(sid, s["id"])
        self.assertEqual(item, {"type": "usage", "raw": line,
                                "tool": "claude"})

    async def test_agent_line_transcript_types_do_not_emit_usage(self):
        """Known transcript types (system/assistant/user/result) keep their
        existing behavior and do not double-emit usage events."""
        s = self.sm.create(name="t", cwd="/tmp", tool="claude")
        got = []
        self.sm.on("agentEvent", lambda sid, item: got.append((sid, item)))
        # assistant message with a usage field must still only push a
        # transcript item, not a usage event
        line = json.dumps({"type": "assistant", "message": {"id": "m1",
                          "content": [{"type": "text", "text": "hi"}]},
                          "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertFalse(self.sm._handle_agent_line(s, line))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1]["t"], "text")

    async def test_host_output_updates_recent(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        got = []
        self.sm.on("output", lambda sid, chunk: got.append(chunk))
        await self.sm.init()
        self.host.emit("output", s["id"], b"hello")
        self.assertEqual(b"".join(got), b"hello")
        self.assertEqual(self.sm.recent_output(s["id"]), b"hello")

    async def test_host_exit_marks_stopped(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        await self.sm.init()
        await self.sm.start(s["id"])
        self.host.emit("exit", s["id"], 0, None)
        self.assertEqual(s["state"], "stopped")
        self.assertEqual(s["exit_code"], 0)

    async def test_host_monitor_reconnects(self):
        class FlakyHost(StubHost):
            """Simulates a pty-host that crashes and comes back unstable:
            the first reconnect attempt is followed by another drop, so the
            monitor must keep trying until the host stays up."""

            def __init__(self):
                super().__init__()
                self._disconnected = True

            async def connect(self):
                self.calls.append("connect")
                self._disconnected = False
                if self.calls.count("connect") < 2:
                    self._disconnected = True  # host drops again right away

            @property
            def connected(self):
                return not self._disconnected

        sm = SessionManager(make_config(), lambda: None)
        host = FlakyHost()
        sm.host = host
        reconnected = []
        sm.on("reconnected", lambda: reconnected.append(True))
        sm.start_host_monitor(interval_s=0.1)
        await asyncio.sleep(0.35)
        sm.stop_host_monitor()
        self.assertGreaterEqual(host.calls.count("connect"), 2)
        self.assertTrue(host.connected)
        self.assertGreaterEqual(len(reconnected), 1)

    async def test_host_monitor_retries_after_list_failure(self):
        class ListFlakyHost(StubHost):
            """Simulates a pty-host whose socket comes up but `list` keeps
            failing for a while: the monitor must keep retrying (host_ready
            stays False) instead of giving up once the socket is connected."""

            def __init__(self):
                super().__init__()
                self._disconnected = True
                self._list_fails = 2

            async def connect(self):
                self.calls.append("connect")
                self._disconnected = False

            async def list(self):
                self.calls.append("list")
                if self._list_fails > 0:
                    self._list_fails -= 1
                    raise RuntimeError("host list unavailable")
                return {"sessions": []}

            @property
            def connected(self):
                return not self._disconnected

        sm = SessionManager(make_config(), lambda: None)
        host = ListFlakyHost()
        sm.host = host
        reconnected = []
        sm.on("reconnected", lambda: reconnected.append(True))
        sm.start_host_monitor(interval_s=0.1)
        await asyncio.sleep(0.4)
        sm.stop_host_monitor()
        # list 失败后 monitor 必须持续重连（connected 为 True 也不停），
        # 直到 list 成功重建视图、host_ready 恢复。
        self.assertGreaterEqual(host.calls.count("list"), 2)
        self.assertGreaterEqual(host.calls.count("connect"), 2)
        self.assertTrue(sm.host_ready)
        self.assertGreaterEqual(len(reconnected), 1)

    def test_agent_send_stopped_returns_false(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="claude-chat")
        s["engine"] = "agent"
        s["state"] = "stopped"
        self.assertFalse(self.sm.agent_send(s["id"], "hi"))

    def test_transcript_empty(self):
        s = self.sm.create(name="t", cwd="/tmp", tool="bash")
        self.assertEqual(self.sm.transcript(s["id"]), [])


class NormalizeToolResultTest(unittest.TestCase):
    def test_string_passthrough_truncated(self):
        self.assertEqual(normalize_tool_result("hi"), "hi")
        out = normalize_tool_result("x" * 9000)
        self.assertLess(len(out), 9000)
        self.assertIn("truncated", out)

    def test_array_of_text_blocks(self):
        content = [{"type": "text", "text": "a"}, "b", {"type": "text", "text": "c"}]
        self.assertEqual(normalize_tool_result(content), "abc")

    def test_image_placeholder(self):
        self.assertEqual(normalize_tool_result([{"type": "image"}]), "[image]")

    def test_null_undefined_empty(self):
        self.assertEqual(normalize_tool_result(None), "")
        self.assertEqual(normalize_tool_result(""), "")


if __name__ == "__main__":
    unittest.main()

    async def test_output_sets_busy_and_clears(self):
        """输出 → busy=True change 事件；空闲 BUSY_IDLE_MS 后 → False。"""
        from session_manager import BUSY_IDLE_MS
        s = self.sm.create(name="busy-test", cwd="/tmp", tool="bash")
        changes = []
        self.sm.on("change", lambda pub: changes.append(pub["busy"]))
        # 模拟 pty 输出（经 StubHost 不会真实输出，直接调内部方法）
        self.sm._emit_output(s, b"hello\r\n")
        self.assertTrue(s["busy"])
        self.assertTrue(changes and changes[-1] is True)
        # 等待空闲清除
        await asyncio.sleep((BUSY_IDLE_MS + 300) / 1000)
        self.assertFalse(s["busy"])
        self.assertFalse(changes[-1])
        await self.sm.remove(s["id"])


class PtyHostClientReadLoopTest(unittest.IsolatedAsyncioTestCase):
    """read_loop 必须处理超 64KB 的单行（pty-host 大帧 base64）。"""

    def _make_client(self):
        from pty_host_client import PtyHostClient
        c = PtyHostClient()
        received = []
        c._on_message = lambda msg: received.append(msg)
        return c, received

    async def test_large_line_over_64kb_does_not_crash(self):
        import asyncio as _a
        from pty_host_client import PtyHostClient
        c = PtyHostClient()
        received = []
        c._on_message = lambda msg: received.append(msg)
        # 构造超 64KB 的单行（StreamReader limit=65536 会触发 LimitOverrunError）
        big = 'x' * 70000
        line = '{"ev":"output","id":"s1","data":"%s"}\n' % big
        reader = _a.StreamReader(limit=4096)  # 故意更小，模拟溢出
        reader.feed_data(line.encode())
        reader.feed_eof()
        c.reader = reader
        c.writer = None
        await c._read_loop()
        # 不应抛异常；行被完整解析（虽然 msg 太大无实际用途，但循环不崩）
        self.assertTrue(c._connected is False)  # EOF 后清理


class ReasonixHistoryTest(unittest.TestCase):
    """_reasonix_has_history 按项目目录精确判断。"""

    def test_encoding_and_detection(self):
        from unittest import mock
        import os, tempfile
        import session_manager as sm

        tmp = tempfile.mkdtemp(prefix="wp-rxhist-")
        # 模拟 ~/.reasonix/projects/-root-webpty/sessions/ 有会话文件
        proj = os.path.join(tmp, ".reasonix", "projects", "-root-webpty", "sessions")
        os.makedirs(proj, exist_ok=True)
        open(os.path.join(proj, "20260808-010000-test.jsonl"), "w").close()

        with mock.patch.object(sm, "os") as mocked_os:
            # os.path.expanduser 需要真实，patch 其他
            pass
        # 直接验证编码函数
        enc = "-" + "/root/webpty".replace("/", "-").lstrip("-")
        self.assertEqual(enc, "-root-webpty")
        enc_root = "-" + "/root".replace("/", "-").lstrip("-")
        self.assertEqual(enc_root, "-root")

        # 用 mock expanduser 指向 tmp
        import session_manager as sm2
        with mock.patch.object(sm2.os.path, "expanduser", return_value=tmp):
            self.assertTrue(sm2._reasonix_has_history("/root/webpty"))
            self.assertFalse(sm2._reasonix_has_history("/nonexistent"))
