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


class MalformedMessageTest(unittest.TestCase):
    """Audit H1: malformed messages must be ignored, never crash the host."""

    def test_non_json_line_ignored(self):
        pty_host.on_line(None, "this is not json{{{")

    def test_json_non_dict_ignored(self):
        # Previously: msg.get -> AttributeError -> host crash.
        pty_host.on_line(None, "[1, 2, 3]")
        pty_host.on_line(None, "null")
        pty_host.on_line(None, '"str"')

    def test_unknown_op_ignored(self):
        pty_host.on_line(None, '{"op": "no-such-op", "id": "x"}')

    def test_bad_resize_types_ignored(self):
        # Previously: int("abc") -> ValueError -> host crash.
        pty_host.on_line(None, '{"op": "resize", "id": "nope", "cols": "abc"}')
        pty_host.on_line(None, '{"op": "resize", "cols": [1,2]}')

    def test_bad_input_payload_ignored(self):
        pty_host.on_line(None, '{"op": "input", "id": "nope", "data": [1,2]}')

    def test_bad_start_types_ignored(self):
        # Previously: int(None)/ValueError inside handle_start -> crash.
        pty_host.on_line(None, '{"op": "start", "id": "s1", "command": "ls", "cols": "abc"}')
        pty_host.on_line(None, '{"op": "start", "id": "s2", "command": "ls", "args": "notalist"}')
        # handler must still be alive after all the garbage
        pty_host.on_line(None, '{"op": "list"}')


if __name__ == "__main__":
    unittest.main()
