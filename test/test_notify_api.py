import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
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
        st, out = self._req("GET", "/api/notify/messages?page=1")
        self.assertEqual(st, 200)
        self.assertIn("total", out)
        self.assertIn("items", out)


if __name__ == "__main__":
    unittest.main()
