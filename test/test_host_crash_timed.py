"""Isolated pty-host crash test.

Dedicated server + dedicated pty-host on a private socket
(WEBPTY_PTY_HOST_PIPE) — production's shared host is never touched.

Verifies the live WS notification path across a REAL host SIGKILL:
state(stopped) frame -> reconnected frame -> autostart restart ->
state(running) frame -> session functional again (echo roundtrip).
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


class HostCrashIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="webpty-hostcrash-")
        cls.proj_root = os.path.join(cls.data_dir, "projects")
        os.makedirs(os.path.join(cls.proj_root, "alpha"), exist_ok=True)
        cls.port = _pick_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pipe = "/tmp/webpty-hostcrash-%d.sock" % os.getpid()
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
        # kill any leftover dedicated host children
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

    def _host_pids(self):
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(self.proc.pid), "-f", "pty_host"])
            return [int(p) for p in out.split()]
        except subprocess.CalledProcessError:
            return []

    def test_pty_host_crash_live_notify_and_autostart(self):
        import asyncio

        async def run():
            t0 = time.time()
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "bash", "name": "crash1", "autostart": True})
            sid = sess["id"]
            self._req(f"/api/sessions/{sid}/start", "POST")
            ws, head = await self._ws_connect(
                sid, b"i=0; while true; do echo HCRASH$i; i=$((i+1)); sleep 1; done\r")
            print("T t=%.2f ws connected" % (time.time()-t0), flush=True)
            self.assertIn(b"101", head)
            # first ticks arrive
            got = b""
            end = time.time() + 8
            while time.time() < end and b"HCRASH1" not in got:
                frame = await ws.recv(1.5)
                if frame is None:
                    break
                _op, data = frame
                got += data
            print("T t=%.2f HCRASH1 got len=%d" % (time.time()-t0, len(got)), flush=True)
            self.assertIn(b"HCRASH1", got, "ticker did not start")

            hosts = self._host_pids()
            self.assertTrue(hosts, "dedicated pty-host not running")
            os.system("kill -9 %d" % hosts[0])

            # sequence: state(stopped) -> reconnected -> state(running)
            text = b""
            seen_stopped = seen_reconnected = False
            end = time.time() + 40
            while time.time() < end and not (seen_stopped
                                             and seen_reconnected):
                frame = await ws.recv(1.5)
                if frame is None:
                    continue  # recv() returns None on timeout too — keep waiting
                op, data = frame
                if op == 0x1:
                    text += data
                    if b'"stopped"' in text:
                        seen_stopped = True
                    if b'"reconnected"' in text:
                        seen_reconnected = True
            self.assertTrue(seen_stopped,
                            "host crash must surface state(stopped); got: %r"
                            % text[-300:])
            self.assertTrue(seen_reconnected,
                            "host respawn must surface reconnected; got: %r"
                            % text[-300:])
            # strict: running must arrive AFTER the crash (busy-transition
            # state frames during the ticker also contain "running" — reset
            # the accumulator so a stale frame cannot pass the check)
            text = b""
            seen_running = False
            end = time.time() + 40
            while time.time() < end and not seen_running:
                frame = await ws.recv(1.5)
                if frame is None:
                    continue
                op, data = frame
                if op == 0x1:
                    text += data
                    if b'"running"' in text:
                        seen_running = True
            self.assertTrue(seen_running,
                            "autostart restart must surface state(running); got: %r"
                            % text[-300:])

            # session functional again: echo roundtrip
            mask = bytes([17, 34, 51, 68])
            payload = b"echo ALIVE\r"
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            ws.writer.write(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
            await ws.writer.drain()
            out = b""
            end = time.time() + 8
            while time.time() < end and b"ALIVE" not in out:
                frame = await ws.recv(1.0)
                if frame is None:
                    break
                _op, data = frame
                out += data
            self.assertIn(b"ALIVE", out,
                          "session must accept input after host-crash recovery")
            await ws.close()
            # cleanup
            self._req(f"/api/sessions/{sid}/stop", "POST")
            self._req(f"/api/sessions/{sid}", "DELETE")
            st, _ = self._req("/api/health")
            self.assertEqual(st, 200)

    def test_start_session_right_after_host_kill(self):
        # 宿主刚被杀就创建+启动新会话:必须自愈(按需重生宿主),
        # 而不是阻塞或留下死会话——用户操作不应被宿主故障挡住。
        import asyncio

        async def run():
            t0 = time.time()
            hosts = self._host_pids()
            self.assertTrue(hosts, "dedicated pty-host not running")
            os.system("kill -9 %d" % hosts[0])
            time.sleep(0.3)
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "bash", "name": "crash2"})
            self.assertEqual(st, 201, sess)
            sid = sess["id"]
            st, j = self._req(f"/api/sessions/{sid}/start", "POST")
            self.assertEqual(st, 200, j)
            # give the host respawn + spawn time
            state = None
            end = time.time() + 15
            while time.time() < end:
                time.sleep(0.5)
                st, j = self._req(f"/api/sessions/{sid}")
                state = j.get("state")
                if state == "running":
                    break
            self.assertEqual(state, "running",
                             "session must reach running after host respawn; "
                             "last: %r %r" % (state, j.get("last_error")))
            # and it must actually work: echo roundtrip over WS
            CR = chr(13)
            ws, head = await self._ws_connect(
                sid, ("echo HOSTBACK" + CR).encode())
            print("T t=%.2f ws connected" % (time.time()-t0), flush=True)
            self.assertIn(b"101", head)
            out = b""
            end = time.time() + 8
            while time.time() < end and b"HOSTBACK" not in out:
                frame = await ws.recv(1.0)
                if frame is None:
                    continue
                _op, data = frame
                out += data
            await ws.close()
            self.assertIn(b"HOSTBACK", out,
                          "session must accept input after host respawn")
            # cleanup
            self._req(f"/api/sessions/{sid}/stop", "POST")
            self._req(f"/api/sessions/{sid}", "DELETE")
            # host_ready flag lags the monitor cycle after a respawn — poll
            ready = False
            end = time.time() + 12
            while time.time() < end:
                st, j = self._req("/api/health")
                self.assertEqual(st, 200)
                if j.get("host_ready"):
                    ready = True
                    break
                time.sleep(0.5)
            self.assertTrue(ready, "host_ready must recover after respawn")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
