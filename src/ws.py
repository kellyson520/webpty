"""Minimal RFC 6455 WebSocket server implementation (server side only).

Enough for webpty's needs: text and binary frames, ping/pong keepalive,
close handshake. Built on asyncio streams so no third-party dependency.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAGIC = 65536  # 2**16 for mask bit

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(Exception):
    pass


def accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """A connected WebSocket session. Caller uses send_text/send_bytes/
    recv/close; the server drives handshake via `accept()`.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.open = False
        self._recv_buf = bytearray()
        self._closed = False
        self.outbox = None

    # --- outbound ----------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        try:
            self.writer.write(bytes(header) + payload)
        except Exception:  # noqa: BLE001
            self._closed = True

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(_OP_BINARY, data)

    def attach_outbox(self, outbox) -> None:
        """Route fire-and-forget sends through `outbox` (see Outbox).

        Once attached, send_text_async/send_bytes_async enqueue into the
        outbox instead of writing a frame and spawning a per-frame drain
        task; the outbox's single background task drains them.
        """
        self.outbox = outbox

    def send_text_async(self, text: str) -> None:
        """Fire-and-forget send with drain (use from sync callbacks)."""
        if self.outbox is not None:
            self.outbox.send(text, binary=False)
            return
        self.send_text(text)
        if not self._closed:
            try:
                asyncio.get_event_loop().create_task(self.drain())
            except Exception:  # noqa: BLE001
                pass

    def send_bytes_async(self, data: bytes) -> None:
        """Fire-and-forget send with drain (use from sync callbacks)."""
        if self.outbox is not None:
            self.outbox.send(data, binary=True)
            return
        self.send_bytes(data)
        if not self._closed:
            try:
                asyncio.get_event_loop().create_task(self.drain())
            except Exception:  # noqa: BLE001
                pass

    async def drain(self) -> None:
        if not self._closed:
            try:
                await self.writer.drain()
            except Exception:  # noqa: BLE001
                self._closed = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        payload = struct.pack(">H", code) + reason.encode("utf-8")
        self._send_frame(_OP_CLOSE, payload)
        self._closed = True
        try:
            await self.writer.drain()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass

    # --- inbound -------------------------------------------------------------
    async def recv(self, timeout_s: float | None = None) -> tuple[int, bytes] | None:
        """Returns (opcode, payload) or None on close/EOF."""
        while True:
            if len(self._recv_buf) >= 2:
                try:
                    return self._parse_frame()
                except WebSocketError as err:
                    await self.close(1002, str(err))
                    return None
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout=timeout_s)
            except asyncio.TimeoutError:
                return None
            except (ConnectionError, OSError):
                return None
            if not chunk:
                self._closed = True
                return None
            self._recv_buf += chunk

    def _parse_frame(self) -> tuple[int, bytes]:
        b0 = self._recv_buf[0]
        b1 = self._recv_buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            if len(self._recv_buf) < 4:
                raise WebSocketError("frame too short")
            length = struct.unpack(">H", self._recv_buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(self._recv_buf) < 10:
                raise WebSocketError("frame too short")
            length = struct.unpack(">Q", self._recv_buf[2:10])[0]
            offset = 10
        mask_key = None
        if masked:
            if len(self._recv_buf) < offset + 4:
                raise WebSocketError("frame too short")
            mask_key = self._recv_buf[offset:offset + 4]
            offset += 4
        if len(self._recv_buf) < offset + length:
            raise WebSocketError("frame too short")
        payload = bytes(self._recv_buf[offset:offset + length])
        del self._recv_buf[:offset + length]

        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == _OP_PING:
            self._send_frame(_OP_PONG, payload)
            return self._parse_frame()
        if opcode == _OP_PONG:
            return self._parse_frame()
        if opcode == _OP_CLOSE:
            self._closed = True
            return None
        if not fin:
            # Fragmented frames — not needed by the webpty client; treat as
            # a protocol error rather than buffer indefinitely.
            raise WebSocketError("fragmented frames not supported")
        return opcode, payload


async def accept_websocket(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter,
                           request_headers: dict[str, str],
                           request_line: str) -> WebSocket | None:
    """Performs the server handshake. Returns None if the request isn't a
    valid WS upgrade (caller should respond 400/426)."""
    key = request_headers.get("sec-websocket-key")
    if not key:
        return None
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    )
    writer.write(resp.encode("ascii"))
    await writer.drain()
    ws = WebSocket(reader, writer)
    ws.open = True
    return ws


class Outbox:
    """Single-consumer write queue with drop-oldest backpressure.

    Server callbacks call send() synchronously (never blocks the event loop);
    one background task drains the queue and awaits writer.drain().
    """
    def __init__(self, ws, maxlen: int = 1024, drop_oldest: bool = True):
        self.ws = ws
        self.maxlen = maxlen
        self.drop_oldest = drop_oldest
        self.dropped = 0
        self._queue = asyncio.Queue(maxsize=maxlen)
        self._task = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_event_loop().create_task(self._drain_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def send(self, data, binary: bool = True) -> None:  # type: ignore[no-untyped-def]
        if self._task is None:
            return
        item = (binary, data)
        if self.drop_oldest:
            while self._queue.full():
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    break
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _drain_loop(self) -> None:
        try:
            while True:
                binary, data = await self._queue.get()
                if binary:
                    self.ws._send_frame(0x2, data)
                else:
                    self.ws._send_frame(0x1, data.encode("utf-8") if isinstance(data, str) else data)
                if self._queue.empty():
                    await self.ws.drain()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — connection lost; stop silently
            pass
