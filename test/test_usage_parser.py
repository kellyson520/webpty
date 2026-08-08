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
        self.assertEqual(u["cached_in"], 50)

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


def json_line(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    unittest.main()
