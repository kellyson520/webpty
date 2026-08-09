"""Unit tests for the gate boundary in server._handle_request.

Static SPA assets must load WITHOUT authorization (the front-end needs to
render the token-unlock screen), while /api/* requests must be gated.
Regression for: "access denied page shown instead of the unlock UI" —
the gate was answering bare HTML/JS requests with 403 before the JS ran.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from server import Server  # noqa: E402


class FakeReader:
    def __init__(self, target):
        self.lines = [f"GET {target} HTTP/1.1\r\n".encode(),
                      b"Host: x\r\n", b"\r\n"]
        self.i = 0

    async def readline(self):
        if self.i < len(self.lines):
            line = self.lines[self.i]
            self.i += 1
            return line
        return b""


class FakeWriter:
    def __init__(self):
        self.data = []
        self.status_line = None

    def write(self, data):
        self.data.append(data)
        if self.status_line is None and data.startswith(b"HTTP/"):
            self.status_line = data.split(b"\r\n")[0].decode()
        return len(data)

    async def drain(self):
        pass

    def close(self):
        pass


class GateBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, target):
        s = Server()
        s._authorize_calls = []
        s._routed = []

        async def fake_authorize(reader, headers, url):
            s._authorize_calls.append(url)
            return {"ok": False, "reason": "bad-token", "peer": {"ip": "112.224.25.126"}}

        async def fake_route(method, target_, headers, reader, writer):
            s._routed.append(target_)

        s._authorize = fake_authorize
        s._route = fake_route
        w = FakeWriter()
        await s._handle_request(FakeReader(target), w)
        return s, w

    async def test_static_asset_skips_authorize(self):
        s, w = await self._serve("/app.js")
        self.assertEqual(s._authorize_calls, [], "静态资源不得触发 authorize")
        self.assertEqual(s._routed, ["/app.js"], "静态资源应直接路由")
        self.assertNotIn("403", w.status_line or "")

    async def test_index_html_skips_authorize(self):
        s, _ = await self._serve("/")
        self.assertEqual(s._authorize_calls, [], "首页不得触发 authorize")
        self.assertEqual(s._routed, ["/"])

    async def test_api_requires_authorize(self):
        s, w = await self._serve("/api/config")
        self.assertEqual(s._authorize_calls, ["/api/config"], "API 必须触发 authorize")
        self.assertEqual(s._routed, [], "未授权 API 不得路由")
        self.assertIn("403", w.status_line or "")

    async def test_api_rejects_with_json_reason(self):
        s, w = await self._serve("/api/config")
        body = b"".join(w.data)
        self.assertIn(b"bad-token", body)
        self.assertIn(b"forbidden", body)

    async def test_unhandled_exception_returns_500(self):
        """未捕获异常 → 500 JSON 通用文案(而非连接重置或泄露内部细节)。"""
        s = Server()
        s.config["roots"] = ["/root"]  # fs/list 根目录守卫放行,让 mock 真正被调用

        async def fake_authorize(reader, headers, url):
            return {"ok": True}

        s._authorize = fake_authorize
        orig = Server._list_dir_entries

        def boom(self, raw):
            raise ValueError("boom")

        Server._list_dir_entries = boom
        try:
            w = FakeWriter()
            await s._handle_request(FakeReader("/api/fs/list?path=/root"), w)
        finally:
            Server._list_dir_entries = orig
        self.assertIn("500", w.status_line or "")
        body = b"".join(w.data)
        self.assertIn(b"error", body)
        self.assertNotIn(b"boom", body)


if __name__ == "__main__":
    unittest.main()
