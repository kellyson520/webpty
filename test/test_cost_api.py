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


class CostApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp-capi-")
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

    def test_cost_summary_and_grouped(self):
        st, out = self._req("GET", "/api/cost/summary?period=day")
        self.assertEqual(st, 200)
        self.assertIn("cost", out)
        st, out = self._req("GET", "/api/cost/by-tool?period=day")
        self.assertEqual(st, 200)
        self.assertIsInstance(out, list)

    def test_budget_roundtrip(self):
        st, out = self._req("PUT", "/api/cost/budget", {"limit": 12.5})
        self.assertTrue(out["ok"])
        st, out = self._req("GET", "/api/cost/alerts")
        self.assertIsInstance(out, list)

    def test_reconcile_runs(self):
        st, out = self._req("POST", "/api/cost/reconcile")
        self.assertEqual(st, 200)
        self.assertIn("added", out)


if __name__ == "__main__":
    unittest.main()
