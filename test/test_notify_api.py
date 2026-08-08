import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

SRC = os.path.dirname(os.path.abspath(__file__)).replace("/test", "/src")


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class NotifyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp-napi-")
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

    def _req_err(self, method, path, body=None):
        """Like _req but returns (status, body) for non-2xx responses."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode())

    def test_rules_crud(self):
        st, out = self._req("GET", "/api/notify/rules")
        self.assertEqual(st, 200)
        self.assertEqual(out, {"rules": []})
        st, out = self._req("POST", "/api/notify/rules", {
            "name": "r1", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "critical", "quiet_start": "",
            "quiet_end": "", "enabled": 1})
        self.assertEqual(st, 201)
        rid = out["id"]
        st, rules = self._req("GET", "/api/notify/rules")
        self.assertEqual(len(rules["rules"]), 1)
        st, out = self._req("PUT", f"/api/notify/rules/{rid}",
                            {"id": rid, "name": "r1b", "event_type": "failed",
                             "matcher_json": "{}", "action": "email",
                             "level": "warn", "quiet_start": "",
                             "quiet_end": "", "enabled": 1})
        self.assertTrue(out["ok"])
        st, out = self._req("DELETE", f"/api/notify/rules/{rid}")
        self.assertTrue(out["ok"])
        st, rules = self._req("GET", "/api/notify/rules")
        self.assertEqual(rules, {"rules": []})

    def test_messages_pagination(self):
        # Seed 25 notifications directly in the server's SQLite db so the
        # page boundary (page_size=20) is actually crossed.
        conn = sqlite3.connect(os.path.join(self.tmp, "webpty.db"))
        try:
            conn.executemany(
                "INSERT INTO notifications (ts, event_type, level, title,"
                " body, dedup_key, delivered) VALUES (?,?,?,?,?,?,0)",
                [(float(1000 + i), "failed", "info", f"n{i}", "",
                  f"k{i}") for i in range(25)])
            conn.commit()
        finally:
            conn.close()
        st, page1 = self._req("GET", "/api/notify/messages?page=1")
        self.assertEqual(st, 200)
        self.assertEqual(page1["total"], 25)
        self.assertEqual(len(page1["items"]), 20)
        st, page2 = self._req("GET", "/api/notify/messages?page=2")
        self.assertEqual(st, 200)
        self.assertEqual(len(page2["items"]), 5)
        self.assertNotEqual(page1["items"][0]["id"], page2["items"][0]["id"])

    def test_messages_invalid_page(self):
        st, out = self._req_err("GET", "/api/notify/messages?page=abc")
        self.assertEqual(st, 400)

    def test_notify_test_endpoint(self):
        # No SMTP configured in the throwaway env: must answer ok:false
        # without attempting real mail delivery.
        st, out = self._req("POST", "/api/notify/test")
        self.assertEqual(st, 200)
        self.assertEqual(out, {"ok": False})

    def test_rules_validation(self):
        st, out = self._req_err("POST", "/api/notify/rules", {})
        self.assertEqual(st, 400)
        st, out = self._req_err("POST", "/api/notify/rules", {"name": "x"})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
