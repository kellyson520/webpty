"""Integration tests for src/server.py — HTTP API + WebSocket behavior.

Spawns the real server on a random port with an isolated data dir and
exercises the REST endpoints and WS upgrade path end-to-end.
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


class ServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-srv-test-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        os.makedirs(os.path.join(cls.proj_root, "beta"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env.update({
            "WEBPTY_DATA_DIR": cls.data_dir,
            "WEBPTY_PROJECTS_ROOT": cls.proj_root,
            "WEBPTY_PORT": str(cls.port),
            "WEBPTY_BIND_HOST": "127.0.0.1",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(_ROOT, "src", "server.py")],
            cwd=_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        # Wait for the server to come up.
        for _ in range(50):
            time.sleep(0.1)
            try:
                urllib.request.urlopen(f"{cls.base}/api/config")
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            cls.proc.kill()
            raise RuntimeError("server did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _req(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", method=method, data=data,
            headers=headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:  # noqa: BLE001
                return e.code, {}

    # --- API ---------------------------------------------------------------
    def test_config(self):
        status, j = self._req("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("codex", j["tools"])
        self.assertIn("reasonix", j["tools"])
        self.assertEqual(j["gate"], "none")

    def test_projects_list(self):
        status, j = self._req("/api/projects")
        self.assertEqual(status, 200)
        names = [p["name"] for p in j]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    def test_projects_reject_empty_path(self):
        status, _ = self._req("/api/projects", "POST", {"path": "  "})
        self.assertEqual(status, 400)

    def test_projects_reject_missing_dir(self):
        status, _ = self._req("/api/projects", "POST",
                              {"path": os.path.join(self.data_dir, "missing")})
        self.assertEqual(status, 400)

    def test_create_project_with_git(self):
        status, j = self._req("/api/projects/create", "POST",
                              {"name": "newproj", "gitInit": True})
        self.assertEqual(status, 201)
        self.assertEqual(j["name"], "newproj")
        self.assertTrue(os.path.exists(os.path.join(self.proj_root, "newproj", ".git")))

    def test_create_project_traversal_rejected(self):
        status, j = self._req("/api/projects/create", "POST", {"name": "../evil"})
        self.assertEqual(status, 400)
        self.assertIn("outside", j.get("error", ""))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.proj_root), "evil")))

    def test_create_project_absolute_outside(self):
        status, _ = self._req("/api/projects/create", "POST", {"path": "/etc/hackdir"})
        self.assertEqual(status, 400)

    def test_session_unknown_tool(self):
        status, j = self._req("/api/sessions", "POST",
                              {"cwd": os.path.join(self.proj_root, "alpha"), "tool": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown tool", j.get("error", ""))

    def test_session_outside_roots(self):
        status, j = self._req("/api/sessions", "POST", {"cwd": "/etc", "tool": "bash"})
        self.assertEqual(status, 400)
        self.assertIn("outside", j.get("error", ""))

    def test_session_missing_cwd(self):
        status, j = self._req("/api/sessions", "POST", {"tool": "bash"})
        self.assertEqual(status, 400)
        self.assertIn("cwd", j.get("error", ""))

    def test_session_create_and_list(self):
        status, j = self._req("/api/sessions", "POST",
                              {"cwd": os.path.join(self.proj_root, "alpha"),
                               "tool": "bash", "name": "alpha-shell"})
        self.assertEqual(status, 201)
        self.assertEqual(j["tool"], "bash")
        self.assertEqual(j["state"], "stopped")
        self.assertEqual(j["engine"], "pty")
        _, sessions = self._req("/api/sessions")
        self.assertTrue(any(s["tool"] == "bash" for s in sessions))

    def test_roots_put(self):
        status, j = self._req("/api/config/roots", "PUT", {"roots": [self.proj_root]})
        self.assertEqual(status, 200)
        self.assertEqual(j["roots"], [self.proj_root])

    def test_fs_list(self):
        status, j = self._req("/api/fs/list")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(j), 1)

    def test_fs_list_bad_path(self):
        status, _ = self._req("/api/fs/list?path=/definitely/not/here")
        self.assertEqual(status, 400)

    # --- WebSocket -----------------------------------------------------------
    async def _ws_roundtrip(self, sid, payload):
        reader, writer = await asyncio_open_conn(self.port)
        key = base64.b64encode(b"0123456789abcdef").decode()
        req = (f"GET /ws/sessions/{sid} HTTP/1.1\r\nHost: x\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        writer.write(req.encode())
        await writer.drain()
        head = await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n"):
                break
        ws = WebSocket(reader, writer)
        ws.open = True
        mask = b"\x11\x22\x33\x44"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        writer.write(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
        await writer.drain()
        return ws, head

    def test_ws_echo(self):
        import asyncio

        async def run():
            status, sess = self._req("/api/sessions", "POST",
                                     {"cwd": os.path.join(self.proj_root, "alpha"),
                                      "tool": "bash", "name": "ws-shell"})
            sid = sess["id"]
            self._req(f"/api/sessions/{sid}/start", "POST")
            ws, head = await self._ws_roundtrip(sid, b"echo WS_ECHO_OK\r")
            self.assertIn(b"101", head)
            end = time.time() + 8
            got = b""
            while time.time() < end:
                frame = await ws.recv(1.5)
                if frame is None:
                    break
                _op, data = frame
                got += data
                if b"WS_ECHO_OK" in got:
                    break
            await ws.close()
            self.assertIn(b"WS_ECHO_OK", got)

        asyncio.run(run())

    def test_ws_malformed_session_id_rejected(self):
        import asyncio

        async def run():
            try:
                reader, writer = await asyncio_open_conn(self.port)
                key = base64.b64encode(b"0123456789abcdef").decode()
                req = ("GET /ws/sessions/%zz HTTP/1.1\r\nHost: x\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                writer.write(req.encode())
                await writer.drain()
                head = await reader.readline()
                writer.close()
                self.assertIn(b"400", head)
            finally:
                # Server still alive afterwards.
                status, _ = self._req("/api/config")
                self.assertEqual(status, 200)

        asyncio.run(run())


async def asyncio_open_conn(port):
    import asyncio

    return await asyncio.open_connection("127.0.0.1", port)


if __name__ == "__main__":
    unittest.main()
