"""agent_config.py 单元测试：白名单、读取、TOML 精准替换、JSON 替换。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 — vendored backport
    import tomli as tomllib  # type: ignore[no-redef]

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
            "opencode": [os.path.join(self.home, ".config", "opencode",
                                      "opencode.json")],
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
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["model"], "C:\\models\\gpt5")

    def test_toml_invalid_file_refused(self):
        # 无法解析的 TOML 拒绝编辑，不破坏原文件
        p = self._write(".codex/config.toml", 'model = "unclosed\n[broken\n')
        res = ac.update_config("codex", {"model": "gpt-5.2"})
        self.assertFalse(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn("unclosed", content)  # 原样保留

    def test_toml_array_value_refused(self):
        """Audit M3: array/multiline/table values can't be line-replaced —
        refuse loudly instead of appending a duplicate key (silent no-op)."""
        p = self._write(".codex/config.toml", 'model = ["a", "b"]\n')
        res = ac.update_config("codex", {"model": "gpt-5.2"})
        self.assertFalse(res["ok"])
        self.assertIn("数组", res.get("error", ""))
        content = open(p, encoding="utf-8").read()
        self.assertIn('["a", "b"]', content)  # 原样保留
        self.assertEqual(content.count("model"), 1)  # 无重复键

    def test_update_creates_bak(self):
        """Audit M3: every successful update keeps a .bak of the pre-edit
        file so a mistaken key write can always be reverted."""
        p = self._write(".codex/config.toml", 'model = "gpt-5"\n')
        res = ac.update_config("codex", {"model": "gpt-5.2"})
        self.assertTrue(res["ok"])
        bak = p + ".bak"
        self.assertTrue(os.path.exists(bak))
        with open(bak, encoding="utf-8") as f:
            self.assertIn("gpt-5", f.read())

    def test_explicit_path_direct_contact(self):
        """显式 path 可直连 $HOME 之外的配置文件（隔离放宽，外部直连）。"""
        outer = os.path.join(self.tmp, "outside", "remote.toml")
        os.makedirs(os.path.dirname(outer), exist_ok=True)
        with open(outer, "w", encoding="utf-8") as f:
            f.write('model = "old"\n')
        res = ac.update_config("codex", {"model": "new"}, path=outer)
        self.assertTrue(res["ok"], res)
        content = open(outer, encoding="utf-8").read()
        self.assertIn('model = "new"', content)
        self.assertTrue(os.path.exists(outer + ".bak"))
        rd = ac.read_config("codex", path=outer)
        self.assertTrue(rd["ok"])
        self.assertIn("new", rd["content"])

    def test_explicit_path_missing_file_rejected(self):
        res = ac.update_config("codex", {"model": "x"},
                               path="/nonexistent/xx.toml")
        self.assertFalse(res["ok"])
        res = ac.read_config("codex", path="/nonexistent/xx.toml")
        self.assertFalse(res["ok"])

    def test_section_key_value_escaped(self):
        """model_providers.<id>.base_url 值含引号/反斜杠必须正确转义，
        否则生成非法 TOML（潜在损坏）。"""
        p = self._write(".codex/config.toml",
                        '[model_providers."prov"]\n'
                        'base_url = "https://old/v1"\n')
        raw = 'a"b\\c'
        res = ac.update_config("codex",
                               {"model_providers.prov.base_url": raw})
        self.assertTrue(res["ok"], res)
        content = open(p, encoding="utf-8").read()
        parsed = tomllib.loads(content)  # 转义后必须是合法 TOML
        self.assertEqual(parsed["model_providers"]["prov"]["base_url"], raw)
        self.assertIn('"a\\"b\\\\c"', content)  # 文件里的转义形态

    def test_opencode_json_provider_conversion(self):
        """opencode provider 为字符串时,设置 base_url/api_key 转为对象形式;
        model 直接替换。"""
        p = self._write(".config/opencode/opencode.json",
                        json.dumps({"model": "m1", "provider": "anthropic"}))
        res = ac.update_config("opencode", {"base_url": "https://x/v1",
                                            "model": "m2"})
        self.assertTrue(res["ok"], res)
        obj = json.load(open(p, encoding="utf-8"))
        self.assertEqual(obj["model"], "m2")
        self.assertEqual(obj["provider"]["options"]["baseURL"], "https://x/v1")
        # api_key 写进同一个 options 对象
        res = ac.update_config("opencode", {"api_key": "sk-1"})
        self.assertTrue(res["ok"], res)
        obj = json.load(open(p, encoding="utf-8"))
        self.assertEqual(obj["provider"]["options"]["apiKey"], "sk-1")
        self.assertEqual(obj["provider"]["options"]["baseURL"], "https://x/v1")

    def test_env_secret_reader(self):
        """read_agent_env_secret: 进程环境优先,回退 ~/.reasonix/.env。"""
        with mock.patch.dict(os.environ, {"CODEX_API_KEY": "env-key"}):
            self.assertEqual(ac.read_agent_env_secret("CODEX_API_KEY"),
                             "env-key")
        tmp = tempfile.mkdtemp(prefix="wp-envsec-")
        os.makedirs(os.path.join(tmp, ".reasonix"), exist_ok=True)
        with open(os.path.join(tmp, ".reasonix", ".env"),
                  "w", encoding="utf-8") as f:
            f.write("# comment\nDEEPSEEK_API_KEY = \"file-key\"\nOTHER=x\n")
        with mock.patch.object(ac, "_HOME", tmp), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ac.read_agent_env_secret("DEEPSEEK_API_KEY"),
                             "file-key")
            self.assertIsNone(ac.read_agent_env_secret("MISSING"))

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
        self.assertNotIn("http://proxy:8080", content)  # 旧值被替换
        # temperature 是数值(非字符串):codex 配置期望 TOML 数值
        self.assertIn("temperature = 0.7", content)
        self.assertNotIn('temperature = "0.7"', content)

    def test_reasonix_keys_replaced(self):
        """reasonix api_key/base_url/provider 可编辑。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wp-rkeys-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('default_model = "m1"\nbase_url = "https://old/v1"\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"reasonix": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.update_config("reasonix", {
                    "api_key": "sk-new", "base_url": "https://new/v1",
                    "provider": "openai"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('api_key = "sk-new"', content)
        self.assertIn('base_url = "https://new/v1"', content)
        self.assertIn('provider = "openai"', content)
        self.assertNotIn("https://old/v1", content)
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

    def test_model_providers_section_key_replaced(self):
        tmp = tempfile.mkdtemp(prefix="wp-sec-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.4"\n'
                    '\n'
                    '[model_providers."my-prov"]\n'
                    'base_url = "https://old/v1"\n'
                    'api_key = "sk-old"\n'
                    '\n'
                    '[projects."/root"]\n'
                    'trust_level = "trusted"\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.update_config(
                    "codex", {"model_providers.my-prov.base_url": "https://new/v1"})
        self.assertTrue(res["ok"], res)
        content = open(p, encoding="utf-8").read()
        self.assertIn('base_url = "https://new/v1"', content)
        self.assertIn("api_key = \"sk-old\"", content)  # 其他段键未动
        self.assertIn('[model_providers."my-prov"]', content)  # 段头保留
        self.assertIn('[projects."/root"]', content)  # 其他段未动
