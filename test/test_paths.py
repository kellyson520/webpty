"""Unit tests for src/paths.py — path normalization and root containment."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from paths import case_fold, is_path_under_roots, normalize_fs_path  # noqa: E402


class NormalizeFsPathTest(unittest.TestCase):
    def test_resolves_relative_to_cwd(self):
        self.assertEqual(normalize_fs_path("."), os.getcwd())
        self.assertEqual(normalize_fs_path("./x"), os.path.join(os.getcwd(), "x"))

    def test_strips_trailing_slashes(self):
        self.assertEqual(normalize_fs_path("/tmp/abc///"), "/tmp/abc")
        self.assertEqual(normalize_fs_path("/tmp/abc/"), "/tmp/abc")

    def test_root_stays_root(self):
        self.assertEqual(normalize_fs_path("/"), os.path.sep)

    def test_expands_user(self):
        self.assertEqual(normalize_fs_path("~"), os.path.expanduser("~"))


class IsPathUnderRootsTest(unittest.TestCase):
    def test_direct_child(self):
        self.assertTrue(is_path_under_roots("/tmp/root-a/proj", ["/tmp/root-a"]))

    def test_root_itself(self):
        self.assertTrue(is_path_under_roots("/tmp/root-b", ["/tmp/root-b"]))

    def test_sibling_prefix_not_under(self):
        self.assertFalse(is_path_under_roots("/tmp/root-c2", ["/tmp/root-c"]))

    def test_parent_not_under(self):
        self.assertFalse(is_path_under_roots("/tmp/root-d", ["/tmp/root-d/sub"]))

    def test_deep_nesting(self):
        self.assertTrue(is_path_under_roots("/tmp/root-e/a/b/c", ["/tmp/root-e"]))

    def test_filesystem_root(self):
        self.assertTrue(is_path_under_roots("/etc", ["/"]))

    def test_empty_roots_deny_all(self):
        self.assertFalse(is_path_under_roots("/tmp/x", []))

    def test_any_of_semantics(self):
        self.assertTrue(is_path_under_roots("/tmp/r2/p", ["/tmp/r1", "/tmp/r2"]))
        self.assertFalse(is_path_under_roots("/etc/passwd", ["/tmp/r1", "/tmp/r2"]))

    def test_case_fold(self):
        if sys.platform == "win32":
            self.assertEqual(case_fold("/Root/Web"), "/root/web")
        else:
            self.assertEqual(case_fold("/Root/Web"), "/Root/Web")


if __name__ == "__main__":
    unittest.main()
