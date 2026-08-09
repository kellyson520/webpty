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

    def test_toml_section_key_not_touched(self):
        # 顶层 model 与 [projects] 段内的 model 并存 → 只改顶层
        p = self._write(".codex/config.toml",
                        'model = "gpt-5.4"\n'
                        '[projects."/mnt/TG-ONE"]\n'
                        'model = "local-project-model"\n'
                        'trust_level = "trusted"\n')
        res = ac.update_config("codex", {"model": "gpt-5.2"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('model = "gpt-5.2"', content)
        self.assertIn('model = "local-project-model"', content)  # 段内不动
        self.assertIn('trust_level = "trusted"', content)

    def test_toml_value_escaping(self):
        # 值含反斜杠（Windows 路径）正确转义
        p = self._write(".codex/config.toml", 'model = "gpt-5.4"\n')
        res = ac.update_config("codex", {"model": "C:\\models\\gpt5"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('model = "C:\\\\models\\\\gpt5"', content)
        # 转义后仍是合法 TOML
        import tomllib
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["model"], "C:\\models\\gpt5")

    def test_toml_invalid_file_refused(self):
        # 无法解析的 TOML 拒绝编辑，不破坏原文件
        p = self._write(".codex/config.toml", 'model = "unclosed\n[broken\n')
        res = ac.update_config("codex", {"model": "gpt-5.2"})
        self.assertFalse(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn("unclosed", content)  # 原样保留

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


class MaskTest(unittest.TestCase):
    def test_read_config_masks_secrets(self):
        """raw 配置视图中的 api_key / AUTH_TOKEN 被掩蔽。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wp-mask-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.4"\napi_key = "sk-super-secret-123"\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.read_config("codex")
        self.assertTrue(res["ok"])
        self.assertNotIn("sk-super-secret-123", res["content"])
        self.assertIn("****", res["content"])

    def test_toml_new_keys_replaced(self):
        """codex proxy/temperature 与 reasonix api_key/base_url 可编辑。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wp-keys-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.4"\nproxy = "http://proxy:8080"\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.update_config("codex", {
                    "proxy": "http://new-proxy:3128",
                    "temperature": "0.7"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('proxy = "http://new-proxy:3128"', content)
        # temperature 是数值(非字符串):codex 配置期望 TOML 数值
        self.assertIn("temperature = 0.7", content)
        self.assertNotIn('temperature = "0.7"', content)

    def test_toml_temperature_rejects_non_numeric(self):
        """temperature 非数值时拒绝写入并报错,不落盘。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wp-keys-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.4"\ntemperature = 0.7\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.update_config("codex", {"temperature": "hot"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "temperature must be numeric")
        # 原文件未被改动
        content = open(p, encoding="utf-8").read()
        self.assertIn("temperature = 0.7", content)
