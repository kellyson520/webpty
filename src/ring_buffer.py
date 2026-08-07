"""Byte ring buffer — shared by pty-host (scrollback) and the server
(session recent-output replay). Bytes only: PTY data is UTF-8 but we never
decode here, we slice the tail and re-emit verbatim so escape sequences stay
intact.
"""
from __future__ import annotations


class RingBuffer:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._buf = bytearray(capacity)
        self._write = 0
        self._size = 0

    def push(self, chunk: bytes) -> None:
        n = len(chunk)
        if n == 0:
            return
        if n >= self.capacity:
            # Chunk alone exceeds buffer — keep only the tail.
            self._buf[:] = chunk[-self.capacity:]
            self._write = 0
            self._size = self.capacity
            return
        space = self.capacity - self._write
        if n <= space:
            self._buf[self._write:self._write + n] = chunk
            self._write = (self._write + n) % self.capacity
        else:
            self._buf[self._write:] = chunk[:space]
            self._buf[:n - space] = chunk[space:]
            self._write = n - space
        self._size = min(self._size + n, self.capacity)

    def snapshot(self) -> bytes:
        if self._size == 0:
            return b""
        start = (self._write - self._size) % self.capacity
        if start + self._size <= self.capacity:
            return bytes(self._buf[start:start + self._size])
        first = self.capacity - start
        return bytes(self._buf[start:]) + bytes(self._buf[:self._size - first])
