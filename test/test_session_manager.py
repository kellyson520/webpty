"""Unit tests for src/session_manager.py — SessionManager, RingBuffer,
normalize_tool_result."""
import asyncio
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
