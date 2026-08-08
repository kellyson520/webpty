import unittest

from usage_parser import parse_usage


class UsageParserTest(unittest.TestCase):
    def test_claude_message_start(self):
        line = json_line({"type": "message_start",
                          "message": {"usage": {"input_tokens": 100,
                                                "cache_creation_input_tokens": 50}}})
        u = parse_usage(line, "claude")
        self.assertIsNotNone(u)
        self.assertEqual(u["tokens_in"], 100)
        # cache_creation_input_tokens = 缓存写(按 input 价),归 cached_write
        self.assertEqual(u["cached_write"], 50)
        self.assertEqual(u["cached_in"], 0)

    def test_claude_message_delta(self):
        line = json_line({"type": "message_delta",
                          "usage": {"output_tokens": 200}})
        u = parse_usage(line, "claude")
        self.assertEqual(u["tokens_out"], 200)

    def test_generic_usage_event(self):
        line = json_line({"type": "usage",
                          "usage": {"input_tokens": 10, "output_tokens": 20,
                                    "input_tokens_cached": 3}})
        u = parse_usage(line, "reasonix")
        self.assertEqual((u["tokens_in"], u["tokens_out"], u["cached_in"]),
                         (10, 20, 3))

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_usage("not json at all", "claude"))
        self.assertIsNone(parse_usage('{"type": "ping"}', "claude"))

    def test_reasonix_stats_event(self):
        line = json_line({"type": "stats", "model": "deepseek-v4-flash",
                          "tokens_in": 5, "tokens_out": 6})
        u = parse_usage(line, "reasonix")
        self.assertIsNotNone(u)
        self.assertEqual(u["model"], "deepseek-v4-flash")
        self.assertEqual((u["tokens_in"], u["tokens_out"]), (5, 6))

    def test_bad_int_coerces_to_none(self):
        line = json_line({"type": "usage",
                          "usage": {"input_tokens": "N/A"}})
        self.assertIsNone(parse_usage(line, "claude"))
        line = json_line({"type": "usage",
                          "usage": {"output_tokens": [1, 2]}})
        self.assertIsNone(parse_usage(line, "claude"))

    def test_message_usage_fallback(self):
        line = json_line({"type": "message_start",
                          "message": {"usage": {"input_tokens": 7},
                                       "model": "claude-4"}})
        u = parse_usage(line, "claude")
        self.assertIsNotNone(u)
        self.assertEqual(u["tokens_in"], 7)
        self.assertEqual(u["model"], "claude-4")

    def test_cache_only_event_accepted(self):
        line = json_line({"type": "usage",
                          "usage": {"input_tokens_cached": 7}})
        u = parse_usage(line, "reasonix")
        self.assertIsNotNone(u)
        self.assertEqual(u["cached_in"], 7)

    def test_non_string_input_returns_none(self):
        self.assertIsNone(parse_usage(None, "claude"))
        self.assertIsNone(parse_usage(42, "claude"))

    def test_codex_prompt_completion_fields(self):
        """OpenAI/codex 的 prompt_tokens/completion_tokens 必须被解析。"""
        u = parse_usage('{"type":"usage","usage":{"prompt_tokens":100,'
                        '"completion_tokens":50,"total_tokens":150}}', "codex")
        self.assertIsNotNone(u)
        self.assertEqual(u["tokens_in"], 100)
        self.assertEqual(u["tokens_out"], 50)

    def test_cache_read_and_write_split(self):
        """缓存读(cache_read_input_tokens)与缓存写(cache_creation)分开计。"""
        u = parse_usage('{"usage":{"input_tokens":100,"output_tokens":10,'
                        '"cache_read_input_tokens":90,'
                        '"cache_creation_input_tokens":5}}', "claude")
        self.assertIsNotNone(u)
        self.assertEqual(u["cached_in"], 90)
        self.assertEqual(u["cached_write"], 5)

    def test_message_array_no_crash(self):
        """message 为数组(非 dict)时 parse_usage 不得抛异常。"""
        u = parse_usage('{"message":["x"],"usage":{"input_tokens":1,'
                        '"output_tokens":1}}', "claude")
        self.assertTrue(u is None or isinstance(u, dict))



def json_line(obj):
    import json
    return json.dumps(obj)



if __name__ == "__main__":
    unittest.main()
