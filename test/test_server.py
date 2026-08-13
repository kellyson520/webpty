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
import urllib.parse
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
            # agent-config sync includeSecrets 测试用密钥（服务器子进程环境）
            "SYNC_TEST_KEY": "sk-secret-123",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(_ROOT, "src", "server.py")],
            cwd=_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        # Wait for the server to come up. Audit L4: on failure retry ONCE
        # with a fresh port — the port released by _pick_port can be stolen
        # in the TOCTOU window (flaky in parallel CI).
        for _attempt in range(2):
            try:
                for _ in range(50):
                    time.sleep(0.1)
                    try:
                        urllib.request.urlopen(f"{cls.base}/api/config")
                        break
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    raise RuntimeError("server did not come up")
                break  # came up on this attempt
            except Exception:  # noqa: BLE001
                cls.proc.kill()
                cls.proc.wait(timeout=5)
                cls.port = _pick_port()
                cls.base = f"http://127.0.0.1:{cls.port}"
                env["WEBPTY_PORT"] = str(cls.port)
                cls.proc = subprocess.Popen(
                    [sys.executable, os.path.join(_ROOT, "src", "server.py")],
                    cwd=_ROOT, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
        else:
            cls.proc.kill()
            raise RuntimeError("server did not come up (2 attempts)")

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
            finally:
                e.close()

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

    def test_roots_put_keeps_projects_root(self):
        # roots 不含 projects_root 时自动并入（否则默认会话会 "outside roots"）
        other = os.path.join(self.proj_root, "other")
        status, j = self._req("/api/config/roots", "PUT", {"roots": [other]})
        self.assertEqual(status, 200)
        self.assertIn(self.proj_root, j["roots"])  # projects_root 被保留
        self.assertIn(other, j["roots"])
        # 显式空数组 = 拒绝一切（合法，不强制并入）
        status, j = self._req("/api/config/roots", "PUT", {"roots": []})
        self.assertEqual(status, 200)
        self.assertEqual(j["roots"], [])
        # 清理：恢复项目根，避免影响后续测试
        self._req("/api/config/roots", "PUT",
                  {"roots": [self.proj_root]})

    def test_projects_post_dedups_and_delete_removes(self):
        # Audit fix (v27): POST /api/projects must not re-add a root as an
        # extraFolder (duplicate drawer entry), and DELETE must remove
        # mis-added folders.
        import tempfile as _tf
        outside = _tf.mkdtemp(prefix="webpty-outside-")
        try:
            # add an outside folder
            st, j = self._req("/api/projects", "POST", {"path": outside})
            self.assertEqual(st, 200)
            extra = [p for p in j if p["path"] == outside]
            self.assertEqual(len(extra), 1)
            self.assertTrue(extra[0]["removable"])
            # adding it again must not duplicate
            st, j = self._req("/api/projects", "POST", {"path": outside})
            self.assertEqual(st, 200)
            self.assertEqual([p for p in j if p["path"] == outside], extra)
            # a registered root's child is in the list but not removable;
            # re-POSTing it must not add it to extraFolders
            child = os.path.join(self.proj_root, "alpha")
            st, j = self._req("/api/projects", "POST", {"path": child})
            self.assertEqual(st, 200)
            self.assertFalse([p for p in j if p["path"] == child][0]["removable"])
            # DELETE on a non-extra entry → 404
            st, j = self._req("/api/projects", "DELETE", {"path": child})
            self.assertEqual(st, 404)
            # DELETE the outside folder
            st, j = self._req("/api/projects", "DELETE", {"path": outside})
            self.assertEqual(st, 200)
            self.assertNotIn(outside, [p["path"] for p in j["extraFolders"]])
            # second DELETE → 404
            st, j = self._req("/api/projects", "DELETE", {"path": outside})
            self.assertEqual(st, 404)
            # DELETE on a root → 404
            st, j = self._req("/api/projects", "DELETE", {"path": self.proj_root})
            self.assertEqual(st, 404)
        finally:
            import shutil as _sh
            _sh.rmtree(outside, ignore_errors=True)

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
        # 新增自定义工具:command 必须为内置命令(安全白名单)
        st, j = self._req("/api/config/tools", "PUT",
                          {"tools": {"my-agent": {"command": "bash",
                                                  "defaultArgs": "--x"}}})
        self.assertEqual(st, 200)
        self.assertEqual(j["tools"]["my-agent"]["command"], "bash")
        # 非内置 command 被拒绝(防 RCE)
        st, _ = self._req("/api/config/tools", "PUT",
                          {"tools": {"evil": {"command": "/bin/sh"}}})
        self.assertEqual(st, 400)
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
        self.assertEqual(cfg["tools"]["my-agent"]["command"], "bash")

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
        # Security (Issue 3.5.6): enumeration is restricted to registered
        # roots — an outside path is denied with 403 before any filesystem
        # access.
        status, j = self._req("/api/fs/list?path=/definitely/not/here")
        self.assertEqual(status, 403)
        self.assertIn("outside", j.get("error", ""))

    def test_fs_list_missing_path_inside_root(self):
        # Inside a registered root but nonexistent → OSError → 400.
        status, _ = self._req(
            "/api/fs/list?path=" + urllib.parse.quote(
                os.path.join(self.proj_root, "__nope__")))
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
        # roots 保护：projects_root 自动并入（新行为），other 也在
        self.assertIn(other, drifted["roots"])
        self.assertIn(self.proj_root, drifted["roots"])
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

    def test_index_injects_versioned_assets_and_caches_them(self):
        # /index.html 引用 app.js/styles.css 时带 ?v=<content-hash>
        with urllib.request.urlopen(f"{self.base}/index.html") as resp:
            html = resp.read().decode()
        self.assertIn('src="/app.js?v=', html)
        self.assertIn('href="/styles.css?v=', html)
        # 无版本参数的 app.js 保持 no-store（旧缓存可被部署失效）
        with urllib.request.urlopen(f"{self.base}/app.js") as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        # 带正确 hash 的版本化请求 → immutable
        ver = html.split('src="/app.js?v=', 1)[1].split('"', 1)[0]
        with urllib.request.urlopen(f"{self.base}/app.js?v={ver}") as resp:
            self.assertIn("immutable", resp.headers.get("Cache-Control", ""))
        # 错误 hash → 不缓存（防陈旧引用长期驻留）
        with urllib.request.urlopen(f"{self.base}/app.js?v=deadbeef") as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_font_never_gzip_compressed(self):
        # WOFF2 is already compressed; gzip attempts (100-300ms sync) are
        # skipped AND negatively cached so repeat requests never re-try.
        import http.client
        for _ in range(2):  # second request hits the negative cache
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request("GET", "/fonts/D2Coding.woff2",
                         headers={"Accept-Encoding": "gzip"})
            resp = conn.getresponse()
            resp.read()
            self.assertIsNone(resp.headers.get("Content-Encoding"))
            self.assertEqual(resp.status, 200)
            conn.close()

    def test_etag_304_not_modified(self):
        # 首次带 ETag 响应;第二次 If-None-Match → 304 空 body。
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/app.js")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        etag = resp.getheader("ETag")
        self.assertTrue(etag)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/app.js", headers={"If-None-Match": etag})
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 304)
        self.assertEqual(body, b"")
        conn.close()

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

    def test_sessions_get_single(self):
        # REST 语义:按 id 取单个会话(此前对已存在会话返回 404)。
        st, sess = self._req("/api/sessions", "POST",
                             {"cwd": os.path.join(self.proj_root, "alpha"),
                              "tool": "bash", "name": "single-get"})
        sid = sess["id"]
        st, j = self._req("/api/sessions/" + sid)
        self.assertEqual(st, 200)
        self.assertEqual(j["id"], sid)
        self.assertEqual(j["name"], "single-get")
        st, j = self._req("/api/sessions/00000000-0000-0000-0000-000000000000")
        self.assertEqual(st, 404)
        self._req("/api/sessions/" + sid, "DELETE")

    def test_cost_budget_get_put(self):
        # GET budget 此前 404;PUT 后 GET 反映新值。
        st, j = self._req("/api/cost/budget")
        self.assertEqual(st, 200)
        self.assertIn("limit", j)
        st, j = self._req("/api/cost/budget", "PUT", {"limit": 42.5})
        self.assertEqual(st, 200)
        st, j = self._req("/api/cost/budget")
        self.assertEqual(st, 200)
        self.assertEqual(j["limit"], 42.5)
        # 持久化
        cfg = json.load(open(os.path.join(self.data_dir, "config.json"),
                             encoding="utf-8"))
        self.assertEqual(cfg.get("budget", {}).get("limit"), 42.5)
        self._req("/api/cost/budget", "PUT", {"limit": 0})

    def test_ws_missing_session_404(self):
        # 不存在会话的 WS 升级必须在握手前 404(此前 101 后静默)。
        import asyncio

        async def run():
            reader, writer = await asyncio_open_conn(self.port)
            key = base64.b64encode(b"0123456789abcdef").decode()
            writer.write(("GET /ws/sessions/00000000-0000-0000-0000-000000000000 "
                          "HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                          "Sec-WebSocket-Version: 13\r\n\r\n" % key).encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readline(), 3)
            writer.close()
            return head

        head = asyncio.run(run())
        self.assertIn(b"404", head)
        self.assertNotIn(b"101", head)

    def test_oversized_request_line_414(self):
        # 超过 64KB 的请求行 → 414(此前 LimitOverrunError → 500)。
        s = socket.socket()
        s.connect(("127.0.0.1", self.port))
        s.sendall(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 8192:
                    break
        except OSError:
            pass
        s.close()
        self.assertIn(b"414", data)

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

    def test_ws_reconnect_resumes_output(self):
        # 核心功能:WS 断线后 PTY 会话继续运行,重连后实时输出必须继续
        # (此前 reconnect 不去重窗口吞掉 len(recent) 字节的实时输出)。
        import asyncio

        async def run():
            st, sess = self._req(
                "/api/sessions", "POST",
                {"cwd": os.path.join(self.proj_root, "alpha"),
                 "tool": "bash", "name": "ws-reconn"})
            sid = sess["id"]
            self._req(f"/api/sessions/{sid}/start", "POST")
            ws1, head = await self._ws_roundtrip(
                sid, b"i=0; while true; do echo TICK$i; i=$((i+1)); sleep 1; done\r")
            self.assertIn(b"101", head)
            got = b""
            end = time.time() + 12
            while time.time() < end:
                frame = await ws1.recv(1.5)
                if frame is None:
                    break
                _op, data = frame
                got += data
                if b"TICK2" in got:
                    break
            await ws1.close()
            # 重连:应收到继续的 TICK(而非静默/重复)
            ws2, head2 = await self._ws_roundtrip(sid, b"")
            self.assertIn(b"101", head2)
            got2 = b""
            end = time.time() + 12
            while time.time() < end:
                frame = await ws2.recv(1.5)
                if frame is None:
                    break
                _op, data = frame
                got2 += data
                if b"TICK7" in got2:
                    break
            await ws2.close()
            return got, got2

        g1, g2 = asyncio.run(run())
        self.assertIn(b"TICK2", g1)
        self.assertIn(b"TICK7", g2)

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

    def test_agent_config_read_returns_ok(self):
        """/api/agent-config/read 路由不再 AttributeError(此前必现连接重置)。"""
        # 用存在的工具(codex)请求;即使本机无配置文件也应返回 200 + ok=False
        st, j = self._req("/api/agent-config/read?tool=codex")
        self.assertEqual(st, 200)
        self.assertIn("ok", j)



    def test_agent_config_unknown_tool_400(self):
        """未知工具名 → 400(而非 500/重置)。"""
        st, j = self._req("/api/agent-config/read?tool=nope")
        self.assertEqual(st, 400)
        self.assertIn("error", j)

    # --- audit M6: endpoint smoke coverage --------------------------------
    def test_health_endpoint(self):
        st, j = self._req("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertTrue(j["db"])

    def test_errors_endpoint(self):
        st, j = self._req("/api/errors")
        self.assertEqual(st, 200)
        self.assertIsInstance(j.get("errors"), list)

    def test_cost_export_endpoint(self):
        # returns text/csv, not JSON
        import urllib.request as _ur
        try:
            with _ur.urlopen(f"{self.base}/api/cost/export") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/csv", resp.headers.get("Content-Type", ""))
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            self.fail(f"export failed: {e.code}")
        self.assertIn("session_id", body)  # header row

    def test_session_lifecycle_endpoints(self):
        # create → stop → start → interrupt → reset → delete, plus input
        st, j = self._req("/api/sessions", "POST",
                          {"name": "lc", "cwd": os.path.join(self.proj_root, "alpha"),
                           "tool": "bash"})
        self.assertIn(st, (200, 201))
        sid = j["id"]
        # stop on a stopped session is idempotent (no 500)
        st, j = self._req(f"/api/sessions/{sid}/stop", "POST")
        self.assertIn(st, (200, 404))
        # input on a stopped pty session is a clean 409
        st, j = self._req(f"/api/sessions/{sid}/input", "POST", {"bytes": "echo hi\n"})
        self.assertIn(st, (200, 409))
        # interrupt on a non-running session is a clean 409
        st, j = self._req(f"/api/sessions/{sid}/interrupt", "POST")
        self.assertIn(st, (200, 409))
        # reset is agent-only; pty returns 409 without 500
        st, j = self._req(f"/api/sessions/{sid}/reset", "POST")
        self.assertIn(st, (200, 409))
        # delete
        st, j = self._req(f"/api/sessions/{sid}", "DELETE")
        self.assertEqual(st, 200)
        st, j = self._req(f"/api/sessions/{sid}", "DELETE")
        self.assertEqual(st, 404)

    def test_transcript_export_endpoint(self):
        """Audit L1/M7 (v25-v26): pty sessions have no transcript file →
        404; deleted/unknown sessions → 404 (no 500)."""
        import urllib.error as _ue
        import urllib.request as _ur
        for sid in ("no-such-id",):
            try:
                _ur.urlopen(f"{self.base}/api/sessions/{sid}/transcript")
                self.fail("expected 404")
            except _ue.HTTPError as e:
                self.assertEqual(e.code, 404)
        # a real pty session also has no transcript → 404 (graceful)
        st, j = self._req("/api/sessions", "POST",
                          {"name": "tx", "cwd": os.path.join(self.proj_root, "alpha"),
                           "tool": "bash"})
        sid = j["id"]
        try:
            _ur.urlopen(f"{self.base}/api/sessions/{sid}/transcript")
            self.fail("expected 404 for pty session")
        except _ue.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_notify_read_endpoints(self):
        """Audit M5/M7 (v24-v26): read-all works and unread count drops."""
        import urllib.error as _ue
        import urllib.request as _ur
        # produce a notification (completed event)
        await_sessions = self._req("/api/sessions", "POST",
                                   {"name": "nt", "cwd": os.path.join(self.proj_root, "alpha"),
                                    "tool": "bash"})
        self.assertIn(await_sessions[0], (200, 201))
        # POST read-all is idempotent
        st, j = self._req("/api/notify/read-all", "POST")
        self.assertEqual(st, 200)
        self.assertIn("updated", j)

    def test_fs_list_restricted_to_roots(self):
        # roots 内路径允许
        st, _ = self._req(f"/api/fs/list?path={self.proj_root}")
        self.assertEqual(st, 200)
        # roots 外路径拒绝（403）
        st, _ = self._req("/api/fs/list?path=/etc")
        self.assertEqual(st, 403)
        # 空 path（系统根浏览起点）允许
        st, _ = self._req("/api/fs/list")
        self.assertEqual(st, 200)

    def test_fs_list_null_byte_400(self):
        # 路径含 NUL 字节 → realpath/scandir 抛 ValueError；必须 400 而非
        # 500（根内路径走完整守卫后到达 scandir）。
        bad = os.path.join(self.proj_root, "a\x00b")
        st, _ = self._req("/api/fs/list?path=" + urllib.parse.quote(bad))
        self.assertEqual(st, 400)
        st, _ = self._req("/api/projects", "POST",
                          {"path": "/tmp/a\x00b"})
        self.assertEqual(st, 400)

    def test_fs_list_symlink_escape_blocked(self):
        # roots 内指向外部的符号链接不能被枚举（realpath 二次校验）。
        link = os.path.join(self.proj_root, "alpha", "escape")
        try:
            os.symlink("/etc", link)
        except OSError:
            self.skipTest("symlinks unavailable")
        st, j = self._req("/api/fs/list?path=" + urllib.parse.quote(link))
        self.assertEqual(st, 403)
        self.assertIn("outside", j.get("error", ""))

    def test_agent_config_read_update_explicit_path(self):
        # 显式 path 直连任意配置文件（隔离放宽），.bak 备份仍保留。
        tmp = tempfile.mkdtemp(prefix="wp-ext-")
        cfg = os.path.join(tmp, "remote.toml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('model = "old"\n')
        st, j = self._req("/api/agent-config/read?tool=codex&path="
                          + urllib.parse.quote(cfg))
        self.assertEqual(st, 200)
        self.assertIn("old", j["content"])
        st, j = self._req("/api/agent-config/update", "PUT",
                          {"tool": "codex", "values": {"model": "new"},
                           "path": cfg})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertIn('model = "new"', open(cfg, encoding="utf-8").read())
        self.assertTrue(os.path.exists(cfg + ".bak"))

    def test_agent_config_sync_include_secrets(self):
        # includeSecrets 时导入 env 密钥;响应绝不回显密钥值;默认不导入。
        tmp = tempfile.mkdtemp(prefix="wp-syncsec-")
        cfg = os.path.join(tmp, "config.toml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('model_provider = "mine"\n'
                    '[model_providers.mine]\n'
                    'base_url = "https://sec.example/v1"\n'
                    'env_key = "SYNC_TEST_KEY"\n')
        st, j = self._req("/api/agent-config/sync", "POST",
                          {"tool": "codex", "path": cfg,
                           "includeSecrets": True})
        self.assertEqual(st, 200)
        self.assertTrue(any("apiKey" in c for c in j["changed"]), j)
        self.assertNotIn("sk-secret-123", json.dumps(j))  # 不回显密钥
        # API 响应掩蔽 apiKey;磁盘上是真实值
        st, j = self._req("/api/config")
        self.assertEqual(j["providers"]["mine"]["apiKey"], "****-123")
        on_disk = json.load(open(os.path.join(self.data_dir, "config.json"),
                                 encoding="utf-8"))
        self.assertEqual(on_disk["providers"]["mine"]["apiKey"],
                         "sk-secret-123")
        # 不带 includeSecrets 的后续同步不动密钥
        st, j = self._req("/api/agent-config/sync", "POST",
                          {"tool": "codex", "path": cfg})
        self.assertEqual(st, 200)
        self.assertFalse(any("apiKey" in c for c in j["changed"]), j)
        on_disk = json.load(open(os.path.join(self.data_dir, "config.json"),
                                 encoding="utf-8"))
        self.assertEqual(on_disk["providers"]["mine"]["apiKey"],
                         "sk-secret-123")

    def test_agent_config_sync_opencode(self):
        # opencode.json 的 provider.options.baseURL + model 同步进注册表。
        tmp = tempfile.mkdtemp(prefix="wp-ocsync-")
        cfg = os.path.join(tmp, "opencode.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"model": "om-1",
                       "provider": {"options":
                                    {"baseURL": "https://oc.example/v1"}}}, f)
        st, j = self._req("/api/agent-config/sync", "POST",
                          {"tool": "opencode", "path": cfg})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        st, j = self._req("/api/config")
        found = next((p for p in j["providers"].values()
                      if p.get("baseUrl") == "https://oc.example/v1"), None)
        self.assertIsNotNone(found)
        self.assertIn("om-1", found.get("models", []))
        self.assertEqual(j["tools"]["opencode"]["provider"], "opencode")

    def test_agent_config_sync_imports_provider(self):
        # codex config.toml 的 [model_providers] 段可同步进 webpty 的
        # providers 注册表 + tools.codex.provider 映射。
        tmp = tempfile.mkdtemp(prefix="wp-sync-")
        cfg = os.path.join(tmp, "config.toml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('model_provider = "mine"\nmodel = "m-1"\n'
                    '[model_providers.mine]\n'
                    'base_url = "https://sync.example/v1"\n')
        st, j = self._req("/api/agent-config/sync", "POST",
                          {"tool": "codex", "path": cfg})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        st, j = self._req("/api/config")
        self.assertEqual(j["providers"]["mine"]["baseUrl"],
                         "https://sync.example/v1")
        self.assertEqual(j["tools"]["codex"]["provider"], "mine")
        # 幂等：第二次同步无变化
        st, j = self._req("/api/agent-config/sync", "POST",
                          {"tool": "codex", "path": cfg})
        self.assertEqual(st, 200)
        self.assertEqual(j["changed"], [])

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





class XtermPatchTest(unittest.TestCase):
    """Audit 5.1: the vendored xterm 5.5.0 carries a manual touch patch;
    an unpatched upgrade would silently regress mobile scrolling. Assert the
    patched byte patterns so CI/upgrades fail loudly."""

    def test_xterm_touch_patch_preserved(self):
        path = os.path.join(_ROOT, "public", "vendor", "xterm", "lib", "xterm.js")
        with open(path, "rb") as f:
            src = f.read().decode("utf-8", errors="replace")
        # 1) touchmove unconditionally scrolls (patched form).
        self.assertIn(
            'touchmove",(e=>{return this.viewport.handleTouchMove(e)?void 0:this.cancel(e)}),{passive:!1})',
            src)
        # 2) touchstart never cancels (patched form: no areMouseEventsActive gate).
        self.assertIn(
            'touchstart",(e=>{this.viewport.handleTouchStart(e)}),{passive:!0})',
            src)
        # 3) PATCHES.md documents the change.
        self.assertTrue(os.path.isfile(
            os.path.join(_ROOT, "public", "vendor", "PATCHES.md")))


if __name__ == "__main__":
    unittest.main()
