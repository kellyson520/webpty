"""Isolated agent-CLI crash test.

Dedicated server + dedicated pty-host on a private socket; the agent
engine runs a FAKE stream-json CLI (test/fake_agent.py) so nothing real
is touched. This mirrors production exactly: codex/reasonix are agent
engine sessions — their CLI processes die on crashes and webpty must
surface state frames, autostart-restart, and keep the transcript alive.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
from ws import WebSocket  # noqa: E402

FAKE_AGENT = os.path.join(_HERE, "fake_agent.py")


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AgentCrashIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-agentcrash-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        config = {
            "tools": {
                "bash": {"command": "bash", "defaultArgs": "",
                         "nameFlag": None},
                "fake-agent": {
                    "command": FAKE_AGENT, "defaultArgs": "",
                    "nameFlag": None, "engine": "agent",
                    "permissionMode": "bypassPermissions",
                },
            },
            "sessions": [],
        }
        with open(os.path.join(cls.data_dir, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(config, f)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-agentcrash-%d.sock" % os.getpid()
        env = dict(os.environ)
        env.update({
            "WEBPTY_DATA_DIR": cls.data_dir,
            "WEBPTY_PROJECTS_ROOT": cls.proj_root,
            "WEBPTY_PORT": str(cls.port),
            "WEBPTY_BIND_HOST": "127.0.0.1",
            "WEBPTY_PTY_HOST_PIPE": cls.pipe,
        })
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(_ROOT, "src", "server.py")],
            cwd=_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        cls.base + "/api/health", timeout=2) as resp:
                    j = json.loads(resp.read())
                    if j.get("ok") and j.get("host_ready"):
                        return
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.3)
        cls.proc.kill()
        raise RuntimeError("test server did not become healthy")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                cls.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        # leftover dedicated host + fake agents
        for pat in ("pty_host", "fake_agent.py"):
            try:
                out = subprocess.check_output(
                    ["pgrep", "-P", str(cls.proc.pid), "-f", pat])
                for pid in out.split():
                    os.system("kill -9 %s 2>/dev/null" % pid.decode())
            except Exception:  # noqa: BLE001
                pass
        for path in (cls.pipe, cls.pipe + ".lock"):
            try:
                os.unlink(path)
            except OSError:
                pass
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:  # noqa: BLE001
                return e.code, {}

    async def _ws_connect(self, sid):
        import asyncio
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        key = base64.b64encode(b"0123456789abcdef").decode()
        CRLF = chr(13) + chr(10)
        req = ("GET /ws/sessions/" + sid + " HTTP/1.1" + CRLF +
               "Host: x" + CRLF + "Upgrade: websocket" + CRLF +
               "Connection: Upgrade" + CRLF +
               "Sec-WebSocket-Key: " + key + CRLF +
               "Sec-WebSocket-Version: 13" + CRLF + CRLF)
        writer.write(req.encode())
        await writer.drain()
        head = await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n"):
                break
        ws = WebSocket(reader, writer)
        ws.open = True
        return ws, head

    def _agent_pids(self):
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(self.proc.pid), "-f", "fake_agent.py"])
            return [int(p) for p in out.split()]
        except subprocess.CalledProcessError:
            return []


    def test_meta_noop_guard(self):
        # Regression guard: a test whose coroutine is never awaited passes
        # instantly (the asyncio.run(run()) line was once lost to an edit).
        # Every async test method must contain the run() call.
        import inspect
        import re as _re
        src = inspect.getsource(type(self))
        for name, _ in inspect.getmembers(type(self), inspect.isfunction):
            if not name.startswith("test_") or name == "test_meta_noop_guard":
                continue
            m = _re.search(
                r"def %s\(self\):.*?asyncio\.run\(run\(\)\)" % name,
                src, _re.S)
            self.assertIsNotNone(
                m,
                "%s must call asyncio.run(run()) — silent no-op guard" % name)

    def test_agent_cli_crash_live_notify_autostart_and_resume(self):
        import asyncio

        async def run():
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "fake-agent", "name": "agcrash", "autostart": True})
            self.assertEqual(st, 201, sess)
            sid = sess["id"]
            self._req(f"/api/sessions/{sid}/start", "POST")
            ws, head = await self._ws_connect(sid)
            self.assertIn(b"101", head)

            # snapshot + live ticks
            text = b""
            end = time.time() + 8
            while time.time() < end and not (b'"snapshot"' in text
                                             and b"TICK" in text):
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                op, data = frame
                if op == 0x1:
                    text += data
            self.assertIn(b'"snapshot"', text, "agent snapshot missing")
            self.assertIn(b"TICK", text, "agent live ticks missing")

            agents = self._agent_pids()
            self.assertTrue(agents, "fake agent CLI not running")
            os.system("kill -9 %d" % agents[0])

            # require the sequence stopped -> running (a "running" frame
            # from BEFORE the kill would otherwise pass the check)
            seen_stopped = False
            text = b""
            end = time.time() + 40
            while time.time() < end:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                op, data = frame
                if op != 0x1:
                    continue
                text += data
                if not seen_stopped:
                    if b'"stopped"' in text:
                        seen_stopped = True
                        text = b""  # fresh accumulator for the running search
                elif b'"running"' in text:
                    break
            self.assertTrue(seen_stopped,
                            "agent crash must surface state(stopped); got: %r"
                            % text[-300:])
            self.assertTrue(b'"running"' in text,
                            "autostart must restart agent -> state(running); got: %r"
                            % text[-300:])

            # fresh process emits ticks again
            text = b""
            end = time.time() + 12
            while time.time() < end and b"TICK" not in text:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                op, data = frame
                if op == 0x1:
                    text += data
            self.assertIn(b"TICK", text,
                          "restarted agent must emit new output")

            # user message roundtrip over the live WS (proper masked frame)
            mask = bytes([17, 34, 51, 68])
            payload = b'{"type":"user","__ctl":true,"text":"hello42"}'
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            ws.writer.write(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
            await ws.writer.drain()
            text = b""
            end = time.time() + 6
            while time.time() < end and b"GOT: hello42" not in text:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                op, data = frame
                if op == 0x1:
                    text += data
            self.assertIn(b"GOT: hello42", text,
                          "user message must roundtrip after crash recovery")
            await ws.close()
            # cleanup
            self._req(f"/api/sessions/{sid}/stop", "POST")
            self._req(f"/api/sessions/{sid}", "DELETE")
            st, _ = self._req("/api/health")
            self.assertEqual(st, 200)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
