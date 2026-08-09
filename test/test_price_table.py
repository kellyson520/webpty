import unittest

from price_table import DEFAULT_PRICES, cost_for, get_price


class PriceTableTest(unittest.TestCase):
    def test_defaults_present(self):
        for m in ("claude", "codex", "reasonix", "opencode", "deepseek"):
            self.assertIn(m, DEFAULT_PRICES)

    def test_unknown_model_falls_back(self):
        p = get_price("mystery-model", {})
        self.assertEqual(p["input"], 1.0)
        self.assertEqual(p["output"], 2.0)

    def test_prefix_matching(self):
        # 真实模型 ID 前缀匹配家族价
        self.assertEqual(get_price("claude-opus-4-8", {})["input"], 15.0)
        self.assertEqual(get_price("gpt-5.4", {})["input"], 1.25)
        # Audit M3 (v23): exact entries beat the family prefix.
        self.assertEqual(get_price("deepseek-v4-flash", {})["input"], 0.02)
        self.assertEqual(get_price("deepseek-v4-pro", {})["input"], 0.27)
        self.assertEqual(get_price("gpt-5.2", {})["input"], 0.50)
        self.assertEqual(get_price("claude-opus-4-5", {})["input"], 5.0)
        # 家族回退
        self.assertEqual(get_price("claude-anything-new", {})["input"], 3.0)
        # gemini 家族
        self.assertAlmostEqual(get_price("gemini-2.5-pro", {})["input"], 0.15)

    def test_exact_version_prices_beat_family(self):
        # Audit E1: dated/versioned ids hit the exact entries (longest
        # prefix), correcting the family price where it was wrong.
        self.assertEqual(get_price("claude-haiku-4-5-20251001", {})["input"], 1.0)
        self.assertEqual(get_price("gpt-4o-mini", {})["input"], 0.15)
        self.assertEqual(get_price("gpt-5-mini-latest", {})["input"], 0.25)
        self.assertEqual(get_price("gpt-5-nano", {})["input"], 0.05)
        self.assertEqual(get_price("codex-mini-latest", {})["input"], 0.25)
        self.assertEqual(get_price("deepseek-chat", {})["input"], 0.28)

    def test_config_override_prefix_matches(self):
        # Audit E2: a configured prefix covers dated ids too.
        cfg = {"prices": {"claude-haiku-4-5": {"input": 2.0, "output": 8.0,
                                               "cache_hit": 0.2, "currency": "USD"}}}
        p = get_price("claude-haiku-4-5-20251001", cfg)
        self.assertEqual(p["input"], 2.0)

    def test_config_overrides(self):
        cfg = {"prices": {"claude": {"input": 99.0, "output": 99.0,
                                     "cache_hit": 0.0, "currency": "CNY"}}}
        p = get_price("claude", cfg)
        self.assertEqual(p["input"], 99.0)
        self.assertEqual(p["currency"], "CNY")

    def test_cost_calculation(self):
        cfg = {"prices": {"m": {"input": 10.0, "output": 20.0,
                                "cache_hit": 1.0, "currency": "USD"}}}
        c = cost_for("m", 1_000_000, 500_000, cfg)
        self.assertAlmostEqual(c, 20.0, places=6)  # 10 + 10
        c2 = cost_for("m", 1_000_000, 500_000, cfg, cached_in=1_000_000)
        self.assertAlmostEqual(c2, 11.0, places=6)  # 1 + 10

    def test_cost_clamps_negative(self):
        cfg = {"prices": {"m": {"input": 10.0, "output": 20.0,
                                "cache_hit": 1.0, "currency": "USD"}}}
        self.assertEqual(cost_for("m", 0, 0, cfg), 0.0)


if __name__ == "__main__":
    unittest.main()
