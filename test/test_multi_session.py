"""Mass multi-session isolation stress (isolated server + dedicated host).

30 concurrent bash sessions on ONE pty-host: per-session output isolation
(each WS sees only its own session's output), full lifecycle, and zero
orphan processes after mass delete.
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

N = 30


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MultiSessionIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-multi-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-multi-%d.sock" % os.getpid()
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
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(cls.proc.pid), "-f", "pty_host"])
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

    def _req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:  # noqa: BLE001
                return e.code, {}

    async def _ws_connect(self, sid, payload=b""):
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
        if payload:
            mask = bytes([17, 34, 51, 68])
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            writer.write(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
            await writer.drain()
        return ws, head


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

    def test_30_sessions_isolation_and_cleanup(self):
        import asyncio

        async def run():
            ids = []
            try:
                for k in range(N):
                    st, sess = self._req(
                        "/api/sessions", "POST",
                        {"cwd": os.path.join(self.proj_root, "alpha"),
                         "tool": "bash", "name": "multi-%02d" % k})
                    self.assertEqual(st, 201, sess)
                    ids.append(sess["id"])
                # start all
                for sid in ids:
                    st, _ = self._req(f"/api/sessions/{sid}/start", "POST")
                    self.assertEqual(st, 200)
                st, j = self._req("/api/sessions")
                self.assertEqual(st, 200)
                running = [s for s in j if s.get("state") == "running"]
                self.assertEqual(len(running), N,
                                 "all %d sessions must be running, got %d"
                                 % (N, len(running)))

                # isolation: feed distinct markers, then check replays
                samples = (0, 7, 15, 22, 29)
                for k in samples:
                    st, _ = self._req(
                        f"/api/sessions/{ids[k]}/input", "POST",
                        {"bytes": "echo MARK%d; sleep 120\n" % k})
                    self.assertEqual(st, 200, "input %d" % k)
                time.sleep(1.2)
                for k in samples:
                    ws, head = await self._ws_connect(ids[k])
                    self.assertIn(b"101", head)
                    got = b""
                    end = time.time() + 5
                    while time.time() < end and b"MARK%d" % k not in got:
                        frame = await ws.recv(1.0)
                        if frame is None:
                            continue
                        _op, data = frame
                        got += data
                    await ws.close()
                    self.assertIn(b"MARK%d" % k, got,
                                  "session %d must replay its own marker" % k)
                    # strict isolation: no OTHER sample's marker
                    for other in samples:
                        if other != k:
                            self.assertNotIn(b"MARK%d" % other, got,
                                             "session %d leaked marker %d"
                                             % (k, other))
            finally:
                for sid in ids:
                    self._req(f"/api/sessions/{sid}/stop", "POST")
                    self._req(f"/api/sessions/{sid}", "DELETE")
            # zero orphans after mass delete: no sleep 120 processes remain
            time.sleep(0.5)
            r = os.system("pgrep -f '[s]leep 120' >/dev/null 2>&1")
            self.assertNotEqual(r, 0,
                                "orphan sleep processes after mass delete")
            st, j = self._req("/api/sessions")
            self.assertEqual(st, 200)
            self.assertEqual(len(j), 0, "no sessions should remain")
            st, j = self._req("/api/health")
            self.assertEqual(st, 200)
            self.assertTrue(j["host_ready"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
