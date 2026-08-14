"""Server-restart reattach test (the 2026-08-14 incident scenario).

Kill the webpty server (SIGKILL) while its pty-host and a running pty
session survive; start a NEW server on the same data dir + pipe. The
session must reattach with the SAME pid, keep its output, and accept
input — no state loss.
"""
import base64
import json
import os
import shutil
import signal
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


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ReattachAfterServerRestartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-ratt-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-ratt-%d.sock" % os.getpid()
        cls.env = dict(os.environ)
        cls.env.update({
            "WEBPTY_DATA_DIR": cls.data_dir,
            "WEBPTY_PROJECTS_ROOT": cls.proj_root,
            "WEBPTY_PORT": str(cls.port),
            "WEBPTY_BIND_HOST": "127.0.0.1",
            "WEBPTY_PTY_HOST_PIPE": cls.pipe,
        })
        cls.proc = None

    @classmethod
    def _spawn_server(cls):
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(_ROOT, "src", "server.py")],
            cwd=_ROOT, env=cls.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        cls.base + "/api/health", timeout=2) as resp:
                    j = json.loads(resp.read())
                    if j.get("ok") and j.get("host_ready"):
                        return True
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.3)
        return False

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.proc is not None:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
                try:
                    cls.proc.stderr.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        # leftover host
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", cls.pipe])
            for pid in out.split():
                os.kill(int(pid), signal.SIGKILL)
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

    def test_reattach_same_pid_after_server_kill(self):
        import asyncio

        async def run():
            self.assertTrue(self._spawn_server(), "server A did not start")
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "bash", "name": "ratt"})
            self.assertEqual(st, 201, sess)
            sid = sess["id"]
            st, _ = self._req(f"/api/sessions/{sid}/start", "POST")
            self.assertEqual(st, 200)
            CR = chr(13)
            st, _ = self._req(f"/api/sessions/{sid}/input", "POST",
                              {"bytes": "echo BEFORERESTART" + CR})
            self.assertEqual(st, 200)
            time.sleep(1.0)
            st, j = self._req(f"/api/sessions/{sid}")
            self.assertEqual(st, 200)
            pid_before = j.get("pid")
            self.assertTrue(pid_before, "session must have a pid")

            # SIGKILL the server; host + session survive (start_new_session)
            self.proc.kill()
            self.proc.wait(timeout=5)
            self.proc = None
            time.sleep(1.0)
            # host must still be alive
            alive = os.path.exists(self.pipe)
            self.assertTrue(alive, "pty-host must survive server SIGKILL")

            # new server, same data dir + pipe
            self.assertTrue(self._spawn_server(), "server B did not start")
            # session must come back RUNNING with the SAME pid
            state = None
            pid_after = None
            end = time.time() + 15
            while time.time() < end:
                st, j = self._req(f"/api/sessions/{sid}")
                state = j.get("state")
                pid_after = j.get("pid")
                if state == "running":
                    break
                time.sleep(0.5)
            self.assertEqual(state, "running",
                             "session must reattach after server restart; "
                             "last_error: %r" % j.get("last_error"))
            self.assertEqual(pid_after, pid_before,
                             "reattach must preserve the pid (%s -> %s)"
                             % (pid_before, pid_after))

            # history preserved + input works
            ws, head = await self._ws_connect(sid)
            self.assertIn(b"101", head)
            got = b""
            end = time.time() + 6
            while time.time() < end and b"BEFORERESTART" not in got:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                _op, data = frame
                got += data
                if _op == 0x1 and data.startswith(b"{"):
                    try:
                        m = json.loads(data)
                        if m.get("type") == "resync":
                            import base64 as _b64
                            got += _b64.b64decode(m.get("data") or "")
                    except Exception:  # noqa: BLE001
                        pass
            self.assertIn(b"BEFORERESTART", got,
                          "replay must contain pre-restart output")
            mask = bytes([17, 34, 51, 68])
            payload = ("echo AFTERRESTART" + CR).encode()
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            ws.writer.write(bytes([0x81, 0x80 | len(payload)])
                            + mask + masked)
            await ws.writer.drain()
            out = b""
            end = time.time() + 6
            while time.time() < end and b"AFTERRESTART" not in out:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                _op, data = frame
                out += data
            self.assertIn(b"AFTERRESTART", out,
                          "input must work after reattach")
            await ws.close()
            # cleanup
            self._req(f"/api/sessions/{sid}/stop", "POST")
            self._req(f"/api/sessions/{sid}", "DELETE")
            st, j = self._req("/api/health")
            self.assertEqual(st, 200)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
