"""agent_config.py 单元测试：白名单、读取、TOML 精准替换、JSON 替换。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import agent_config as ac  # noqa: E402


class AgentConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-acfg-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        # 让 agent_config 以临时 home 为基准（路径检查用）
        patcher = mock.patch.object(ac, "_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._paths_patch = mock.patch.object(ac, "AGENT_CONFIG_PATHS", self._paths())
        self._paths_patch.start()
        self.addCleanup(self._paths_patch.stop)

    def _paths(self):
        return {
            "codex": [os.path.join(self.home, ".codex", "config.toml")],
            "claude": [os.path.join(self.home, ".claude", "settings.json")],
            "reasonix": [os.path.join(self.home, ".reasonix", "config.toml")],
        }

    def _write(self, rel, content):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_list_configs_reports_existence(self):
        self._write(".codex/config.toml", 'model = "gpt-5.4"\n')
        out = ac.list_configs()
        self.assertTrue(out["codex"]["exists"])
        self.assertEqual(out["codex"]["format"], "toml")
        self.assertTrue(out["codex"]["editable"])
        self.assertFalse(out["reasonix"]["exists"])

    def test_toml_precise_replace_keeps_comments(self):
        p = self._write(".codex/config.toml",
                        '# model comment\nmodel = "gpt-5.4"   # trailing\n'
                        'openai_base_url = "https://old/v1"\n')
        res = ac.update_config("codex", {"model": "gpt-5.2",
                                         "base_url": "https://new/v1"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('# model comment\n', content)
        self.assertIn('model = "gpt-5.2"', content)
        self.assertNotIn('model = "gpt-5.4"', content)
        self.assertIn('openai_base_url = "https://new/v1"', content)
        self.assertNotIn("https://old", content)

    def test_toml_missing_key_gets_appended(self):
        p = self._write(".codex/config.toml", 'model = "gpt-5.4"\n')
        res = ac.update_config("codex", {"api_key": "sk-new"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('api_key = "sk-new"\n', content)

    def test_json_replace_env_values(self):
        p = self._write(".claude/settings.json", json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "https://old",
                    "ANTHROPIC_AUTH_TOKEN": "sk-old"},
            "theme": "dark"}, indent=2))
        res = ac.update_config("claude", {
            "base_url": "https://new", "api_key": "sk-new"})
        self.assertTrue(res["ok"])
        obj = json.load(open(p, encoding="utf-8"))
        self.assertEqual(obj["env"]["ANTHROPIC_BASE_URL"], "https://new")
        self.assertEqual(obj["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-new")
        self.assertEqual(obj["theme"], "dark")  # untouched

    def test_unknown_tool_or_path_rejected(self):
        res = ac.update_config("codex", {"bogus_key": "x"})
        self.assertFalse(res["ok"])  # unsupported key
        res = ac.update_config("nope", {"model": "x"})
        self.assertFalse(res["ok"])

    def test_path_traversal_rejected(self):
        # 候选路径指向 home 之外 → config_path 拒绝
        evil = os.path.join(self.tmp, "outside", "config.toml")
        os.makedirs(os.path.dirname(evil), exist_ok=True)
        with open(evil, "w") as f:
            f.write("x")
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [evil]}):
            self.assertIsNone(ac.config_path("codex"))
            self.assertFalse(ac.update_config("codex", {"model": "x"})["ok"])


if __name__ == "__main__":
    unittest.main()
