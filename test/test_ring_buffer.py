"""Unit tests for src/ring_buffer.py — the shared byte ring buffer."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ring_buffer import RingBuffer  # noqa: E402


class RingBufferTest(unittest.TestCase):
    def test_empty_snapshot(self):
        self.assertEqual(RingBuffer(8).snapshot(), b"")

    def test_order_preserved(self):
        rb = RingBuffer(8)
        rb.push(b"ab")
        rb.push(b"cd")
        rb.push(b"ef")
        self.assertEqual(rb.snapshot(), b"abcdef")

    def test_exact_capacity(self):
        rb = RingBuffer(6)
        rb.push(b"abcdef")
        self.assertEqual(rb.snapshot(), b"abcdef")

    def test_over_capacity_keeps_newest(self):
        rb = RingBuffer(8)
        rb.push(b"abcdefgh")
        rb.push(b"ij")
        self.assertEqual(rb.snapshot(), b"cdefghij")

    def test_wrap_around(self):
        rb = RingBuffer(8)
        rb.push(b"12345678")
        rb.push(b"90ab")
        rb.push(b"cdef")
        self.assertEqual(rb.snapshot(), b"567890abcdef"[-8:])

    def test_chunk_larger_than_capacity(self):
        rb = RingBuffer(4)
        rb.push(b"abcdefghij")
        self.assertEqual(rb.snapshot(), b"ghij")

    def test_empty_chunk_noop(self):
        rb = RingBuffer(4)
        rb.push(b"")
        self.assertEqual(rb.snapshot(), b"")

    def test_repeated_single_chars(self):
        rb = RingBuffer(5)
        for c in b"a b c d e f g h i j k".split(b" "):
            rb.push(c)
        self.assertEqual(rb.snapshot(), b"ghijk")

    def test_snapshot_is_copy(self):
        rb = RingBuffer(8)
        chunk = bytearray(b"hello")
        rb.push(bytes(chunk))
        chunk[0] = ord("X")
        self.assertEqual(rb.snapshot(), b"hello")


if __name__ == "__main__":
    unittest.main()
