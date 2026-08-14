"""Project-folder removal must not kill running sessions.

DELETE /api/projects on an extra folder reports active_sessions and the
session keeps running (Audit M3 v27): the connected WS must keep
receiving output, input must still work, and the session must stay
running — then cleanup must remove it cleanly.
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


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProjectDeleteKeepsSessionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-pjdel-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-pjdel-%d.sock" % os.getpid()
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

    def test_delete_extra_folder_keeps_running_session(self):
        import asyncio

        async def run():
            import tempfile as _tf
            outside = _tf.mkdtemp(prefix="webpty-pjdel-out-")
            sid = None
            try:
                st, j = self._req("/api/projects", "POST", {"path": outside})
                self.assertEqual(st, 200, j)
                st, sess = self._req(
                    "/api/sessions", "POST",
                    {"cwd": outside, "tool": "bash", "name": "keep"})
                self.assertEqual(st, 201, sess)
                sid = sess["id"]
                self._req(f"/api/sessions/{sid}/start", "POST")
                # ticker; \r via chr() to avoid transport-escape issues
                CR = chr(13)
                ws, head = await self._ws_connect(
                    sid, ("i=0; while true; do echo KEEPIT$i; i=$((i+1));"
                          " sleep 1; done" + CR).encode())
                self.assertIn(b"101", head)
                got = b""
                end = time.time() + 8
                while time.time() < end and b"KEEPIT1" not in got:
                    frame = await ws.recv(1.5)
                    if frame is None:
                        continue
                    _op, data = frame
                    got += data
                self.assertIn(b"KEEPIT1", got, "ticker did not start")

                # delete the extra folder
                st, j = self._req("/api/projects", "DELETE",
                                  {"path": outside})
                self.assertEqual(st, 200, j)
                self.assertEqual(j.get("active_sessions"), 1,
                                  "server must report the running session")

                # WS keeps receiving ticks
                got2 = b""
                end = time.time() + 6
                while time.time() < end and b"KEEPIT3" not in got2:
                    frame = await ws.recv(1.0)
                    if frame is None:
                        continue
                    _op, data = frame
                    got2 += data
                self.assertIn(b"KEEPIT3", got2,
                              "session must keep running after folder delete")

                # input still works
                mask = bytes([17, 34, 51, 68])
                payload = ("echo AFTERDELETE" + CR).encode()
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                ws.writer.write(bytes([0x81, 0x80 | len(payload)])
                                + mask + masked)
                await ws.writer.drain()
                got3 = b""
                end = time.time() + 6
                while time.time() < end and b"AFTERDELETE" not in got3:
                    frame = await ws.recv(1.0)
                    if frame is None:
                        continue
                    _op, data = frame
                    got3 += data
                self.assertIn(b"AFTERDELETE", got3,
                              "input must work after folder delete")
                await ws.close()

                st, j = self._req(f"/api/sessions/{sid}")
                self.assertEqual(st, 200)
                self.assertEqual(j["state"], "running")
            finally:
                if sid:
                    self._req(f"/api/sessions/{sid}/stop", "POST")
                    self._req(f"/api/sessions/{sid}", "DELETE")
                shutil.rmtree(outside, ignore_errors=True)
            # host still healthy
            st, j = self._req("/api/health")
            self.assertEqual(st, 200)
            self.assertTrue(j["host_ready"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
