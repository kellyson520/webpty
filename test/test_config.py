"""Unit tests for src/config.py — load/persist/merge, env overrides."""
import json
import os
import sys
import tempfile
import unittest

_TEST_DIR = tempfile.mkdtemp(prefix="webpty-cfg-test-")
os.environ["WEBPTY_DATA_DIR"] = _TEST_DIR
os.environ["WEBPTY_PROJECTS_ROOT"] = os.path.join(_TEST_DIR, "projects")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import config as cfg  # noqa: E402


def _read():
    with open(cfg.config_path, "r", encoding="utf-8") as f:
        return json.load(f)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        # Fresh data dir per test (module constants stay, files are recreated).
        import shutil

        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        os.makedirs(_TEST_DIR, exist_ok=True)
        os.makedirs(os.path.join(_TEST_DIR, "projects"), exist_ok=True)
        os.environ.pop("WEBPTY_PORT", None)

    def tearDown(self):
        import shutil

        shutil.rmtree(_TEST_DIR, ignore_errors=True)

    def test_default_created_on_first_run(self):
        c = cfg.load_config()
        self.assertTrue(os.path.exists(cfg.config_path))
        self.assertEqual(c["bindHost"], "0.0.0.0")
        self.assertIn("claude", c["tools"])
        self.assertIn("codex", c["tools"])
        self.assertIn("reasonix", c["tools"])
        self.assertEqual(len(c["tools"]), 13)

    def test_user_added_tool_preserved(self):
        cfg.load_config()
        raw = _read()
        raw["tools"]["my-agent"] = {"command": "myagent", "defaultArgs": "--custom"}
        cfg.save_config(raw)
        c = cfg.load_config()
        self.assertEqual(c["tools"]["my-agent"]["command"], "myagent")
        # Survives a second load (persisted, not clobbered).
        self.assertEqual(cfg.load_config()["tools"]["my-agent"]["command"], "myagent")

    def test_override_builtin_tool(self):
        cfg.load_config()
        raw = _read()
        raw["tools"]["codex"]["defaultArgs"] = "--full-auto"
        cfg.save_config(raw)
        self.assertEqual(cfg.load_config()["tools"]["codex"]["defaultArgs"], "--full-auto")

    def test_disable_tool_with_null(self):
        cfg.load_config()
        raw = _read()
        raw["tools"]["gemini"] = None
        cfg.save_config(raw)
        c = cfg.load_config()
        self.assertIsNone(c["tools"]["gemini"])
        # Marker persisted — stays disabled across restarts.
        self.assertIsNone(cfg.load_config()["tools"]["gemini"])

    def test_disable_tool_with_false(self):
        cfg.load_config()
        raw = _read()
        raw["tools"]["aider"] = False
        cfg.save_config(raw)
        self.assertIs(cfg.load_config()["tools"]["aider"], False)

    def test_recover_from_corrupt_json(self):
        with open(cfg.config_path, "w") as f:
            f.write("{broken json")
        c = cfg.load_config()
        self.assertIn("codex", c["tools"])
        broken = [x for x in os.listdir(_TEST_DIR) if ".broken-" in x]
        self.assertGreaterEqual(len(broken), 1)

    def test_recover_from_null_config(self):
        with open(cfg.config_path, "w") as f:
            f.write("null")
        c = cfg.load_config()
        self.assertIn("codex", c["tools"])

    def test_recover_from_string_config(self):
        with open(cfg.config_path, "w") as f:
            f.write('"just a string"')
        c = cfg.load_config()
        self.assertIn("codex", c["tools"])

    def test_recover_from_array_config(self):
        with open(cfg.config_path, "w") as f:
            f.write("[1,2,3]")
        c = cfg.load_config()
        self.assertIn("codex", c["tools"])

    def test_explicit_empty_roots_kept(self):
        cfg.load_config()
        raw = _read()
        raw["roots"] = []
        cfg.save_config(raw)
        self.assertEqual(cfg.load_config()["roots"], [])

    def test_user_roots_kept(self):
        cfg.load_config()
        raw = _read()
        raw["roots"] = ["/srv/a", "/srv/b"]
        cfg.save_config(raw)
        self.assertEqual(cfg.load_config()["roots"], ["/srv/a", "/srv/b"])

    def test_allowed_logins_and_token(self):
        cfg.load_config()
        raw = _read()
        raw["allowedLogins"] = ["User@Example.com"]
        raw["authToken"] = "tok"
        cfg.save_config(raw)
        c = cfg.load_config()
        self.assertEqual(c["allowedLogins"], ["user@example.com"])
        self.assertEqual(c["authToken"], "tok")

    def test_effective_port(self):
        self.assertEqual(cfg.effective_port(4791), 4791)
        self.assertEqual(cfg.effective_port("abc"), 4789)
        self.assertEqual(cfg.effective_port(0), 4789)
        self.assertEqual(cfg.effective_port(-1), 4789)
        self.assertEqual(cfg.effective_port(70000), 4789)
        self.assertEqual(cfg.effective_port(None), 4789)

    def test_effective_port_env_wins(self):
        os.environ["WEBPTY_PORT"] = "8888"
        self.assertEqual(cfg.effective_port(4791), 8888)
        os.environ["WEBPTY_PORT"] = "not-a-port"
        self.assertEqual(cfg.effective_port(4791), 4791)
        os.environ.pop("WEBPTY_PORT", None)


if __name__ == "__main__":
    unittest.main()
