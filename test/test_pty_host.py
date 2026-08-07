import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Directly importing pty_host must not start a socket — only __main__ runs main().
import pty_host  # noqa: E402


class AccumulateTest(unittest.TestCase):
    def test_merges_small_chunks(self):
        # 5 small chunks inside one 16ms window -> merged into a single frame
        chunks = [b"a", b"b", b"c", b"d", b"e"]
        result = pty_host.merge_chunks(chunks, max_bytes=32768)
        self.assertEqual(result, [b"abcde"])

    def test_splits_over_max_bytes(self):
        chunks = [b"x" * 20000, b"y" * 20000]
        result = pty_host.merge_chunks(chunks, max_bytes=32768)
        self.assertEqual(len(result), 2)
        self.assertEqual(b"".join(result), b"x" * 20000 + b"y" * 20000)

    def test_splits_oversized_single_chunk(self):
        chunks = [b"x" * 40000]
        result = pty_host.merge_chunks(chunks, max_bytes=32768)
        self.assertEqual(result, [b"x" * 32768, b"x" * 7232])
