import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock

SRC = os.path.dirname(os.path.abspath(__file__)).replace("/test", "/src")


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MigrateApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp-mapi-")
        cls.port = _pick_port()
        env = dict(os.environ)
        env.update({"WEBPTY_DATA_DIR": cls.tmp,
                    "WEBPTY_PROJECTS_ROOT": cls.tmp,
                    "WEBPTY_PORT": str(cls.port),
                    "WEBPTY_BIND_HOST": "127.0.0.1"})
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(SRC, "server.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/config",
                                       timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def test_export(self):
        st, out = self._req("POST", "/api/migrate/export")
        self.assertEqual(st, 201)
        self.assertTrue(out["path"].endswith(".tar.gz"))

    def test_clone_missing_template(self):
        st, out = self._req("POST", "/api/migrate/clone",
                            {"template": "/nonexistent/x.tar.gz"})
        self.assertEqual(st, 200)
        self.assertEqual(out["status"], "error")
        self.assertIn("inside backups", out["message"])

    def test_clone_rejects_path_outside_backups(self):
        # backups 目录外真实存在的文件也不可 clone(防文件 oracle)
        outside = os.path.join(self.tmp, "outside.tar.gz")
        with open(outside, "wb") as f:
            f.write(b"garbage")
        st, out = self._req("POST", "/api/migrate/clone",
                            {"template": outside})
        self.assertEqual(st, 200)
        self.assertEqual(out["status"], "error")
        self.assertIn("inside backups", out["message"])

    def test_clone_accepts_export_inside_backups(self):
        st, out = self._req("POST", "/api/migrate/export")
        self.assertEqual(st, 201)
        st2, out2 = self._req("POST", "/api/migrate/clone",
                              {"template": out["path"]})
        self.assertEqual(st2, 200)
        self.assertEqual(out2["status"], "done")

    def test_download_only_latest_export(self):
        st, out = self._req("POST", "/api/migrate/export")
        self.assertEqual(st, 201)
        fn = out["filename"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/migrate/download/{fn}",
                timeout=5) as r:
            self.assertEqual(r.status, 200)
            data = r.read()
        self.assertGreater(len(data), 0)
        # backups/ 下其他文件(非最近导出)→ 404
        other = "webpty-migrate-19990101-000000.tar.gz"
        with open(os.path.join(self.tmp, "backups", other), "wb") as f:
            f.write(data)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/migrate/download/{other}",
                timeout=5)
            self.fail("expected 404 for non-latest export")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_download_rejects_traversal(self):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/api/migrate/download/..%%2f..%%2fetc%%2fpasswd"
                % self.port, timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_list(self):
        st, out = self._req("GET", "/api/migrate/list")
        self.assertEqual(st, 200)
        self.assertIn("migrations", out)


class MigrateParserTest(unittest.TestCase):
    """Unit tests for the multipart parser (server.parse_multipart):
    byte-exact file content, mode whitelist fallback, missing file field."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WEBPTY_DATA_DIR", tempfile.mkdtemp(prefix="wp-mp-"))
        sys.path.insert(0, SRC)
        from server import parse_multipart
        cls.parse = staticmethod(parse_multipart)

    @staticmethod
    def _body(mode=None, file_value=b"content", filename="pkg.tar.gz",
              boundary="zz"):
        out = b""
        if mode is not None:
            out += (b"--" + boundary.encode() + b"\r\n"
                    b'Content-Disposition: form-data; name="mode"\r\n\r\n'
                    + mode + b"\r\n")
        out += (b"--" + boundary.encode() + b"\r\n"
                b'Content-Disposition: form-data; name="file"; filename="'
                + filename.encode() + b'"\r\n'
                b"Content-Type: application/octet-stream\r\n\r\n"
                + file_value + b"\r\n")
        out += b"--" + boundary.encode() + b"--\r\n"
        return out

    def test_parse_basic(self):
        fn, fb, mode = self.parse(self._body(mode=b"replace", file_value=b"abc"),
                                  "zz")
        self.assertEqual(fn, "pkg.tar.gz")
        self.assertEqual(fb, b"abc")
        self.assertEqual(mode, "replace")

    def test_parse_keeps_trailing_crlf_in_payload(self):
        # payload itself ends in \r\n or \n — only the separator CRLF is
        # stripped, the payload survives byte-for-byte.
        fn, fb, mode = self.parse(self._body(file_value=b"binary\r\n"), "zz")
        self.assertEqual(fb, b"binary\r\n")
        fn, fb, mode = self.parse(self._body(file_value=b"binary\n"), "zz")
        self.assertEqual(fb, b"binary\n")

    def test_parse_mode_fallback(self):
        fn, fb, mode = self.parse(self._body(mode=b"evil"), "zz")
        self.assertEqual(mode, "merge")
        fn, fb, mode = self.parse(self._body(), "zz")  # no mode field
        self.assertEqual(mode, "merge")
        fn, fb, mode = self.parse(self._body(mode=b"dry-run"), "zz")
        self.assertEqual(mode, "dry-run")

    def test_parse_missing_file_field(self):
        body = (b"--zz\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\n"
                b"merge\r\n--zz--\r\n")
        fn, fb, mode = self.parse(body, "zz")
        self.assertIsNone(fn)
        self.assertEqual(fb, b"")
        self.assertEqual(mode, "merge")


class MigrateImportHandlerTest(unittest.IsolatedAsyncioTestCase):
    """_handle_migrate_import error branches + happy path (uploaded bytes
    written byte-exact, mode forwarded, migrator.import_package called)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-mimp-")
        self.migrator = SimpleNamespace(import_package=AsyncMock(
            return_value={"status": "done", "mode": "merge"}))
        sys.path.insert(0, SRC)
        from server import Server
        self.server = Server(data_dir=self.tmp, migrator=self.migrator)

    async def _call(self, body, ct, length=None):
        reader = asyncio.StreamReader()
        reader.feed_data(body)
        reader.feed_eof()
        headers = {"content-type": ct}
        if length is not None:
            headers["content-length"] = str(length)
        return await self.server._handle_migrate_import(reader, headers)

    async def test_import_happy_path(self):
        payload = b"binary\r\n"  # ends with CRLF — must survive intact
        body = (b"--zz\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\n"
                b"merge\r\n"
                b"--zz\r\nContent-Disposition: form-data; "
                b"name=\"file\"; filename=\"pkg.tar.gz\"\r\n"
                b"Content-Type: application/octet-stream\r\n\r\n"
                + payload + b"\r\n--zz--\r\n")
        res = await self._call(body, "multipart/form-data; boundary=zz",
                               len(body))
        self.assertEqual(res, {"status": "done", "mode": "merge"})
        dest, mode = self.migrator.import_package.await_args.args
        self.assertEqual(mode, "merge")
        self.assertTrue(dest.startswith(os.path.join(self.tmp, "uploads")))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), payload)

    async def test_import_oversize(self):
        res = await self._call(b"", "multipart/form-data; boundary=zz",
                               50 * 1024 * 1024 + 1)
        self.assertEqual(res["status"], "error")
        self.assertIn("too large", res["message"])
        self.migrator.import_package.assert_not_awaited()

    async def test_import_missing_boundary(self):
        res = await self._call(b"xx", "multipart/form-data", 2)
        self.assertEqual(res,
                         {"status": "error", "message": "missing boundary"})
        self.migrator.import_package.assert_not_awaited()

    async def test_import_missing_file_field(self):
        body = (b"--zz\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\n"
                b"merge\r\n--zz--\r\n")
        res = await self._call(body, "multipart/form-data; boundary=zz",
                               len(body))
        self.assertEqual(res,
                         {"status": "error", "message": "file field missing"})
        self.migrator.import_package.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
