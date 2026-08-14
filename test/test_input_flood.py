"""Input flood must not lose data (Audit T9).

A large paste into a pty session previously went through ONE os.write()
on a non-blocking master fd: once the kernel pty buffer filled, the
remainder was silently dropped (raw-mode TUIs and big pastes). The host
now queues the remainder and flushes on writability. Verified
byte-exactly: 200KB raw-mode stream into cat > file must land in full.
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

FLOOD_BYTES = 200 * 1024


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class InputFloodNoLossTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-flood-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-flood-%d.sock" % os.getpid()
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
            try:
                cls.proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass
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

    def _frame(self, payload: bytes) -> bytes:
        mask = bytes([17, 34, 51, 68])
        n = len(payload)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr = bytes([0x81, 0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return hdr + mask + masked


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

    def test_200kb_flood_lands_byte_exact(self):
        import asyncio

        async def run():
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "bash", "name": "flood"})
            sid = sess["id"]
            self._req(f"/api/sessions/{sid}/start", "POST")
            ws, head = await self._ws_connect(sid)
            self.assertIn(b"101", head)

            outfile = os.path.join(self.data_dir, "flood-out.bin")
            CR = chr(13)
            # stty raw disables ISIG: Ctrl-C would arrive as literal data.
            # The session stop below SIGKILLs cat to end the run.
            cmd = ("stty raw -echo; cat > " + outfile + CR).encode()
            ws.writer.write(self._frame(cmd))
            await ws.writer.drain()
            time.sleep(1.2)

            blob = bytes(range(256)) * (FLOOD_BYTES // 256)
            self.assertEqual(len(blob), FLOOD_BYTES)
            ws.writer.write(self._frame(blob))
            await ws.writer.drain()
            time.sleep(8.0)  # let the host queue + cat drain

            # stop the session: SIGINT then SIGKILL of the pty group
            self._req(f"/api/sessions/{sid}/stop", "POST")
            time.sleep(1.0)
            got = b""
            end = time.time() + 4
            while time.time() < end:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                _op, data = frame
                got += data
            await ws.close()

            size = os.path.getsize(outfile)
            self.assertGreaterEqual(
                size, FLOOD_BYTES,
                "flooded input must land fully: got %d of %d bytes"
                % (size, FLOOD_BYTES))
            with open(outfile, "rb") as f:
                data = f.read()
            self.assertEqual(
                data[:FLOOD_BYTES], blob,
                "flooded content must match exactly (trailing stop bytes ok)")
            self._req(f"/api/sessions/{sid}/stop", "POST")
            self._req(f"/api/sessions/{sid}", "DELETE")
            st, j = self._req("/api/health")
            self.assertEqual(st, 200)
            self.assertTrue(j["host_ready"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
