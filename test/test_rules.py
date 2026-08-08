import unittest
from datetime import datetime

from rules import match_rule, quiet_hours_active


def ev(**kw):
    base = {"type": "completed", "session_id": "s1", "name": "n1",
            "tool": "claude", "project": "/p", "state": "stopped",
            "exit_code": 0, "signal": None}
    base.update(kw)
    return base


class MatchRuleTest(unittest.TestCase):
    def test_event_type_required(self):
        rule = {"event_type": "failed", "matcher_json": "{}"}
        self.assertTrue(match_rule(rule, ev(type="failed")))
        self.assertFalse(match_rule(rule, ev(type="completed")))

    def test_matcher_subset(self):
        rule = {"event_type": "completed",
                "matcher_json": '{"tool": "claude", "project": "/p"}'}
        self.assertTrue(match_rule(rule, ev()))
        self.assertFalse(match_rule(rule, ev(tool="codex")))
        self.assertFalse(match_rule(rule, ev(project="/other")))

    def test_empty_matcher_matches_all(self):
        rule = {"event_type": "completed", "matcher_json": "{}"}
        self.assertTrue(match_rule(rule, ev(tool="anything", project="/x")))

    def test_bad_matcher_json_is_no_match(self):
        rule = {"event_type": "completed", "matcher_json": "{bad"}
        self.assertFalse(match_rule(rule, ev()))


class QuietHoursTest(unittest.TestCase):
    def test_empty_means_no_quiet(self):
        rule = {"quiet_start": "", "quiet_end": ""}
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 12, 0)))

    def test_within_window(self):
        rule = {"quiet_start": "22:00", "quiet_end": "08:00"}
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 23, 30)))
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 3, 0)))
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 12, 0)))

    def test_non_wrapping_window(self):
        rule = {"quiet_start": "09:00", "quiet_end": "17:00"}
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 10, 0)))
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 20, 0)))


if __name__ == "__main__":
    unittest.main()
