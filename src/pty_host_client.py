"""PTY host client — webpty connects to the pty-host daemon over a Unix
socket. If the daemon isn't running we detach-spawn it (POSIX only).

Line-delimited JSON over a Unix socket; PTY output is base64 in the payload.
Uses asyncio so the HTTP/WS server can await host operations without
blocking its event loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys

from logging_util import log_error

_HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(_HERE, "pty_host.py")

PIPE_NAME = (
    os.environ.get("WEBPTY_PTY_HOST_PIPE")
    or ("/tmp/webpty-pty-host.sock" if os.name == "posix" else "webpty-pty-host")
)


class PtyHostError(Exception):
    pass


class PtyHostClient:
    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.server_version: int | None = None
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._listeners: dict[str, list] = {"output": [], "exit": [], "disconnect": []}

    @property
    def connected(self) -> bool:
        """True while the socket to pty-host is open (see `_read_loop`)."""
        return self._connected

    # --- event listeners ---------------------------------------------------
    def on(self, event: str, cb) -> None:  # type: ignore[no-untyped-def]
        self._listeners.setdefault(event, []).append(cb)

    def off(self, event: str, cb) -> None:  # type: ignore[no-untyped-def]
        self._listeners.setdefault(event, []).remove(cb)

    def _emit(self, event: str, *args) -> None:  # type: ignore[no-untyped-def]
        for cb in list(self._listeners.get(event, [])):
            cb(*args)

    # --- connection ----------------------------------------------------------
    async def connect(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await _ensure_host_running()
            reader, writer = await asyncio.open_unix_connection(PIPE_NAME)
            self.reader = reader
            self.writer = writer
            self._connected = True
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            # Read without StreamReader's 64KB line limit: pty-host frames can
            # exceed it when many 32KB merged chunks flush in one line (base64
            # ≈ 44KB each), which crashed readuntil() with LimitOverrunError
            # and killed the whole host connection — sessions then went
            # unresponsive until the monitor reconnected (the reported
            # "反应不快速" slowness).
            buf = bytearray()
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    line = line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._on_message(msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._connected = False
            if self.writer is not None:
                try:
                    self.writer.close()
                except Exception:  # noqa: BLE001
                    pass
                self.writer = None
            self._emit("disconnect")

    def _on_message(self, msg: dict) -> None:
        req_id = msg.get("reqId")
        if req_id is not None and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if msg.get("ev") == "error":
                fut.set_exception(PtyHostError(msg.get("message") or "host error"))
            else:
                fut.set_result(msg)
            if msg.get("ev") == "attached" and msg.get("replay"):
                replay = base64.b64decode(msg["replay"])
                if replay:
                    # A distinct "replay" event (not "output"): consumers
                    # must treat it as a full-state snapshot — the frontend
                    # wipes and replays instead of appending to content that
                    # is already rendered (which doubled TUI output after a
                    # pty-host reconnect).
                    self._emit("replay", msg.get("id"), replay)
            return
        ev = msg.get("ev")
        if ev == "output":
            data = base64.b64decode(msg.get("data") or "")
            self._emit("output", msg.get("id"), data)
        elif ev == "dropped":
            # pty-host's send buffer overflowed for this server connection —
            # output was NOT silently lost: the host dropped this connection
            # and asks us to resync so the next chunk is a full snapshot.
            self._emit("dropped", msg.get("id"))
        elif ev == "exit":
            self._emit("exit", msg.get("id"), msg.get("code"), msg.get("signal"))
        elif ev == "hello":
            self.server_version = msg.get("version")

    # --- request/response ----------------------------------------------------
    async def _request(self, op: str, payload: dict | None = None,
                       timeout_s: float = 5.0) -> dict:
        if not self._connected:
            await self.connect()
        assert self.writer is not None
        self._req_id += 1
        req_id = self._req_id
        msg = {"op": op, "reqId": req_id, **(payload or {})}
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise PtyHostError(f"host {op} timed out")

    def _send(self, op: str, payload: dict | None = None) -> bool:
        if not self._connected or self.writer is None:
            return False
        try:
            self.writer.write((json.dumps({"op": op, **(payload or {})}) + "\n").encode("utf-8"))
            return True
        except Exception as err:  # noqa: BLE001
            log_error("pty-host-client", err)
            return False

    # --- operations -----------------------------------------------------------
    async def list(self) -> dict:
        return await self._request("list")

    async def start(self, opts: dict) -> dict:
        return await self._request("start", opts)

    async def attach(self, sid: str) -> dict:
        return await self._request("attach", {"id": sid})

    def detach(self, sid: str) -> bool:
        return self._send("detach", {"id": sid})

    async def kill(self, sid: str) -> dict:
        return await self._request("kill", {"id": sid})

    async def forget(self, sid: str) -> dict:
        return await self._request("forget", {"id": sid})

    def input(self, sid: str, data) -> bool:  # type: ignore[no-untyped-def]
        if isinstance(data, bytes):
            raw = data
        else:
            raw = str(data).encode("utf-8")
        return self._send("input", {"id": sid, "data": base64.b64encode(raw).decode("ascii")})

    def resize(self, sid: str, cols: int, rows: int) -> bool:
        return self._send("resize", {"id": sid, "cols": cols, "rows": rows})


# --- host spawn helpers -------------------------------------------------------
async def _try_connect(timeout_s: float = 0.6) -> None:
    async def attempt() -> None:
        reader, writer = await asyncio.open_unix_connection(PIPE_NAME)
        writer.close()
        await writer.wait_closed()

    await asyncio.wait_for(attempt(), timeout=timeout_s)


async def _ensure_host_running() -> None:
    try:
        await _try_connect()
        return
    except (OSError, asyncio.TimeoutError):
        pass
    if os.name != "posix":
        raise PtyHostError(
            "pty-host requires POSIX — on Windows install pywinpty and run "
            "python src/pty_host_windows.py "
            "(pip install -r requirements-windows.txt)"
        )
    # Not up — spawn detached. Do NOT poll 4s here (audit F4: it blocked
    # server startup before the listener even bound); the host monitor in
    # SessionManager retries connect + reattaches, so fail fast and let it.
    import subprocess

    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        subprocess.Popen(
            [sys.executable, HOST_SCRIPT],
            stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True, close_fds=True,
        )
    finally:
        os.close(devnull)
    # One short connect attempt (~300ms) to catch immediate spawn failures,
    # then hand off to the monitor.
    for _ in range(3):
        await asyncio.sleep(0.1)
        try:
            await _try_connect(0.3)
            return
        except (OSError, asyncio.TimeoutError):
            continue
    raise PtyHostError(f"pty-host did not come up at {PIPE_NAME}")
