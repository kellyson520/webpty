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
        self._listeners: dict[str, list] = {"output": [], "exit": [], "disconnect": []}

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
        await _ensure_host_running()
        reader, writer = await asyncio.open_unix_connection(PIPE_NAME)
        self.reader = reader
        self.writer = writer
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            async for line in self.reader:
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
                    self._emit("output", msg.get("id"), replay)
            return
        ev = msg.get("ev")
        if ev == "output":
            data = base64.b64decode(msg.get("data") or "")
            self._emit("output", msg.get("id"), data)
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
        except Exception:  # noqa: BLE001
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
        raise PtyHostError("pty-host requires POSIX")
    # Not up — spawn detached, then poll. Use subprocess (not os.fork) so the
    # child never inherits asyncio's epoll fds.
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
    for _ in range(40):
        await asyncio.sleep(0.1)
        try:
            await _try_connect(0.3)
            return
        except (OSError, asyncio.TimeoutError):
            continue
    raise PtyHostError(f"pty-host did not come up at {PIPE_NAME}")
