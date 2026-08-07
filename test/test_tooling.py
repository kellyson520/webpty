"""Unit tests for src/tooling.py — split_args / resolve_command."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from tooling import resolve_command, split_args  # noqa: E402


class SplitArgsTest(unittest.TestCase):
    def test_splits_whitespace(self):
        self.assertEqual(split_args("a b c"), ["a", "b", "c"])

    def test_collapses_runs(self):
        self.assertEqual(split_args("a    b\t c"), ["a", "b", "c"])

    def test_preserves_empty_quoted_arg(self):
        self.assertEqual(split_args('a "" b'), ["a", "", "b"])

    def test_trailing_empty_quoted_arg(self):
        self.assertEqual(split_args('--name ""'), ["--name", ""])

    def test_lone_empty_quotes(self):
        self.assertEqual(split_args('""'), [""])

    def test_quotes_group_whitespace(self):
        self.assertEqual(split_args('--name "my project"'), ["--name", "my project"])

    def test_single_quotes_group_whitespace(self):
        self.assertEqual(split_args("--name 'my project'"), ["--name", "my project"])

    def test_empty_input(self):
        self.assertEqual(split_args(""), [])
        self.assertEqual(split_args(None), [])

    def test_backslash_literal(self):
        # POSIX: a lone backslash is a path char, not an escape.
        self.assertEqual(split_args("a\\b c"), ["a\\b", "c"])

    def test_backslash_in_quotes_literal(self):
        self.assertEqual(split_args('"C:\\temp\\new"'), ["C:\\temp\\new"])

    def test_mixed_quoting(self):
        self.assertEqual(split_args('x "y z" w'), ["x", "y z", "w"])
        self.assertEqual(split_args('x "y"z'), ["x", "yz"])


class ResolveCommandTest(unittest.TestCase):
    def test_absolute_existing_path(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            self.assertEqual(resolve_command(path), path)
        finally:
            os.unlink(path)

    def test_empty_command(self):
        self.assertIsNone(resolve_command(""))
        self.assertIsNone(resolve_command(None))

    def test_bare_name_falls_back(self):
        cmd = "webpty-definitely-not-a-real-binary-xyz"
        r = resolve_command(cmd)
        self.assertTrue(r == cmd or r is None or r.endswith(cmd))


if __name__ == "__main__":
    unittest.main()
