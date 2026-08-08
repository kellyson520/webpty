"""Integration tests for src/server.py — HTTP API + WebSocket behavior.

Spawns the real server on a random port with an isolated data dir and
exercises the REST endpoints and WS upgrade path end-to-end.
"""
import base64
import gzip as gz
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
from unittest import mock

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

    def test_tools_put_updates_and_disables(self):
        # 修改现有工具 defaultArgs
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"codex": {"defaultArgs": "--full-auto"}}})
        self.assertEqual(st, 200)
        self.assertEqual(j["tools"]["codex"]["defaultArgs"], "--full-auto")
        # 禁用工具（null 标记）
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"gemini": None}})
        self.assertEqual(st, 200)
        self.assertNotIn("gemini", j["tools"])
        # 新增自定义工具
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"my-agent": {"command": "myagent",
                                                  "defaultArgs": "--x"}}})
        self.assertEqual(st, 200)
        self.assertEqual(j["tools"]["my-agent"]["command"], "myagent")
        # 非法值忽略（非 dict / 未知字段不破坏现有）
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"bash": 42, "codex": {"bogus": 1}}})
        self.assertEqual(st, 200)
        self.assertIn("codex", j["tools"])
        self.assertNotIn("bogus", j["tools"]["codex"])
        # 重启后持久化（load_config 合并保留用户编辑）
        cfg = json.load(open(os.path.join(self.data_dir, "config.json")))
        self.assertEqual(cfg["tools"]["codex"]["defaultArgs"], "--full-auto")
        self.assertIsNone(cfg["tools"]["gemini"])
        self.assertEqual(cfg["tools"]["my-agent"]["command"], "myagent")

    def test_tools_field_null_clears_not_disables(self):
        # 字段级 null（如 nameFlag=null）只清除字段，不禁用工具
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"codex": {"nameFlag": None}}})
        self.assertEqual(st, 200)
        self.assertIn("codex", j["tools"])  # 工具仍在
        self.assertNotIn("nameFlag", j["tools"]["codex"])  # 字段被清除

    def test_providers_default_and_put(self):
        # 默认预设存在（config.py DEFAULT_PROVIDERS）
        _, cfg = self._req("/api/config")
        self.assertIn("providers", cfg)
        self.assertIn("anthropic", cfg["providers"])
        # PUT 更新预设
        st, j = self._req("/api/config/providers", "PUT",
                          {"providers": {"anthropic": {"apiKey": "sk-test"},
                                         "my-provider": {"baseUrl": "https://x.example/v1",
                                                         "apiKey": "k", "models": ["m1"]}}})
        self.assertEqual(st, 200)
        self.assertEqual(j["providers"]["anthropic"]["apiKey"], "sk-test")
        self.assertEqual(j["providers"]["my-provider"]["baseUrl"], "https://x.example/v1")
        # 删除预设（null）
        st, j = self._req("/api/config/providers", "PUT",
                          {"providers": {"my-provider": None}})
        self.assertNotIn("my-provider", j["providers"])
        # 持久化
        cfg = json.load(open(os.path.join(self.data_dir, "config.json")))
        self.assertEqual(cfg["providers"]["anthropic"]["apiKey"], "sk-test")

    def test_tools_put_provider_fields(self):
        # 工具可关联 provider + 覆盖 apiBaseUrl/apiKey
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"reasonix": {"provider": "deepseek",
                                                  "apiBaseUrl": "https://custom/v1",
                                                  "apiKey": "sk-custom"}}})
        self.assertEqual(st, 200)
        t = j["tools"]["reasonix"]
        self.assertEqual(t["provider"], "deepseek")
        self.assertEqual(t["apiBaseUrl"], "https://custom/v1")
        self.assertEqual(t["apiKey"], "sk-custom")

    def test_fs_list(self):
        status, j = self._req("/api/fs/list")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(j), 1)

    def test_fs_list_bad_path(self):
        status, _ = self._req("/api/fs/list?path=/definitely/not/here")
        self.assertEqual(status, 400)

    # --- backup restore keeps memory config in sync -----------------------
    def test_restore_syncs_memory_config(self):
        # 备份当前 roots → 改 roots → restore → GET /api/config 反映恢复值
        _, before = self._req("/api/config")
        st, b = self._req("/api/backup/create", "POST")
        self.assertEqual(st, 201)
        bid = b["backup"]["id"]
        other = os.path.join(self.proj_root, "alpha")
        st, _ = self._req("/api/config/roots", "PUT", {"roots": [other]})
        self.assertEqual(st, 200)
        _, drifted = self._req("/api/config")
        self.assertEqual(drifted["roots"], [other])
        st, res = self._req(f"/api/backup/restore/{bid}", "POST")
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"])
        _, after = self._req("/api/config")
        # 内存 config 已同步为恢复值(而非停留在被改动的状态)
        self.assertEqual(after["roots"], before["roots"])
        # 清理:roots 还原为项目根,避免影响其他测试
        self._req("/api/config/roots", "PUT",
                  {"roots": [self.proj_root]})

    # --- gzip ---------------------------------------------------------------
    def test_gzip_static_asset(self):
        req = urllib.request.Request(f"{self.base}/app.js",
                                     headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
            # 解压后是合法 JS
            self.assertIn(b"function", gz.decompress(body))

    def test_gzip_small_response_not_compressed(self):
        # GET /api/sessions 此时为空列表（2 字节 < 1KB）：它按字母序先于
        # 所有创建 session 的测试运行，作为「小响应不压缩」的稳定样本。
        req = urllib.request.Request(f"{self.base}/api/sessions",
                                     headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as resp:
            self.assertIsNone(resp.headers.get("Content-Encoding"))

    def test_gzip_q0_not_compressed(self):
        # RFC 9110: gzip;q=0 显式拒绝 gzip，即使通配符 * 为正值。
        req = urllib.request.Request(f"{self.base}/app.js",
                                     headers={"Accept-Encoding": "gzip;q=0, *;q=1"})
        with urllib.request.urlopen(req) as resp:
            self.assertIsNone(resp.headers.get("Content-Encoding"))

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

    def test_ws_agent_snapshot_via_outbox(self):
        # Agent sessions emit their transcript snapshot over the WS; this
        # exercises _ws_session's Outbox path for text frames end-to-end.
        import asyncio

        async def run():
            status, sess = self._req("/api/sessions", "POST",
                                     {"cwd": os.path.join(self.proj_root, "alpha"),
                                      "tool": "claude-chat", "name": "ws-agent"})
            self.assertEqual(status, 201, sess)
            sid = sess["id"]
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
            self.assertIn(b"101", head)
            ws = WebSocket(reader, writer)
            ws.open = True
            end = time.time() + 8
            got = b""
            while time.time() < end:
                frame = await ws.recv(1.5)
                if frame is None:
                    break
                opcode, data = frame
                if opcode == 0x1:  # snapshot arrives as a text frame
                    got += data
                    if b'"snapshot"' in got:
                        break
            await ws.close()
            self.assertIn(b'"snapshot"', got)

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


class ServerUnitTest(unittest.IsolatedAsyncioTestCase):
    """Pure unit tests for server helper functions (no server process)."""

    def test_backup_settings_non_dict_safe(self):
        from server import _backup_settings
        # backup 段非 dict → 回退默认,不抛 AttributeError
        self.assertEqual(_backup_settings({"backup": "not-a-dict"}),
                         (24.0, 7))
        self.assertEqual(_backup_settings({"backup": None}), (24.0, 7))
        self.assertEqual(_backup_settings({}), (24.0, 7))
        # 正常 dict 解析
        self.assertEqual(_backup_settings({"backup": {"interval_hours": 2,
                                                      "retention": 3}}),
                         (2.0, 3))
        # 非数值 → 回退默认并记日志
        with mock.patch("server.log_error") as le:
            self.assertEqual(_backup_settings({"backup": {"interval_hours": "x"}}),
                             (24.0, 7))
            self.assertTrue(le.called)

    async def test_notify_retry_loop_retries_and_survives_errors(self):
        import asyncio

        from server import _notify_retry_loop
        calls = []

        class FakeNotifier:
            async def send_pending(self):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("smtp down")

        task = asyncio.create_task(_notify_retry_loop(FakeNotifier(),
                                                      interval_s=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 首次异常后循环继续,至少再次调用
        self.assertGreaterEqual(len(calls), 2)


async def asyncio_open_conn(port):
    import asyncio

    return await asyncio.open_connection("127.0.0.1", port)


if __name__ == "__main__":
    unittest.main()
