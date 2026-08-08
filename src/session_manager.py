"""Session manager — owns sessions, spawns PTYs via the pty-host daemon,
and runs the agent engine (stream-json protocol over stdio).

Agent sessions (engine == "agent", e.g. claude-chat) spawn the CLI
in-process with stdio pipes and parse a newline-delimited JSON stream.
PTY sessions are delegated to the detached pty-host so they survive
webpty restarts.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid

from config import logs_dir, safe_name
from logging_util import log_error
from paths import package_root  # noqa: F401  (kept for parity/debug)
from ring_buffer import RingBuffer
from tooling import resolve_command, split_args
from pty_host_client import PtyHostClient

AGENT_MAX_ITEMS = 4000
TOOL_RESULT_MAX = 8000
BUSY_IDLE_MS = 5000  # keep the tab dot blinking this long after the last output
RECENT_BUF_CAP = 128 * 1024
DEFAULT_COLS = 120
DEFAULT_ROWS = 30

RESUME_FLAGS = {"-c", "--continue", "-r", "--resume"}


def normalize_tool_result(content) -> str:  # type: ignore[no-untyped-def]
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text") or "")
                elif b.get("type") == "image":
                    parts.append("[image]")
        text = "".join(parts)
    else:
        text = ""
    if len(text) > TOOL_RESULT_MAX:
        text = text[:TOOL_RESULT_MAX] + f"\n… ({len(text) - TOOL_RESULT_MAX} more chars truncated)"
    return text


def encode_claude_project(p: str) -> str:
    return os.path.abspath(p).replace(":", "-").replace("\\", "-").replace("/", "-").replace("_", "-")


def has_prior_conversation(cwd: str) -> bool:
    import glob

    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    return bool(glob.glob(os.path.join(base, encode_claude_project(cwd), "*.jsonl")))


def _append_log(log_path: str, text: str) -> None:
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


class SessionManager:
    def __init__(self, config: dict, save_cb) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self.save = save_cb
        self.sessions: dict[str, dict] = {}
        self.host = PtyHostClient()
        self.host_sessions: dict[str, dict] = {}
        self.host_ready = False
        self._listeners: dict[str, list] = {"output": [], "agentEvent": [], "change": [], "remove": [], "session_event": []}
        for stored in config.get("sessions", []):
            session = self._inflate(stored)
            self.sessions[session["id"]] = session

    # --- events ---------------------------------------------------------------
    def on(self, event: str, cb) -> None:  # type: ignore[no-untyped-def]
        self._listeners.setdefault(event, []).append(cb)

    def off(self, event: str, cb) -> None:  # type: ignore[no-untyped-def]
        try:
            self._listeners.setdefault(event, []).remove(cb)
        except ValueError:
            pass

    def _emit(self, event: str, *args) -> None:  # type: ignore[no-untyped-def]
        for cb in list(self._listeners.get(event, [])):
            cb(*args)

    # --- lifecycle --------------------------------------------------------------
    async def init(self) -> None:
        self.host.on("output", self._on_host_output)
        self.host.on("exit", self._on_host_exit)
        self.host.on("disconnect", self._on_host_disconnect)
        try:
            await self.host.connect()
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] pty-host connect failed: {err}", flush=True)
            return
        try:
            result = await self.host.list()
            self.host_sessions = {s["id"]: s for s in result.get("sessions", [])}
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] pty-host list failed: {err}", flush=True)
        self.host_ready = True

    def list(self) -> list[dict]:
        return [self._public(s) for s in self.sessions.values()]

    def public(self, sid: str) -> dict | None:
        s = self.sessions.get(sid)
        return self._public(s) if s else None

    def get(self, sid: str) -> dict | None:
        return self.sessions.get(sid)

    def recent_output(self, sid: str) -> bytes | None:
        s = self.sessions.get(sid)
        if not s or not s.get("recent_buf"):
            return None
        return s["recent_buf"].snapshot()

    def create(self, *, name: str, cwd: str, tool: str, args: str = "",
               autostart: bool = False) -> dict:
        sid = str(uuid.uuid4())
        session = self._inflate({
            "id": sid, "name": name or os.path.basename(cwd) or cwd,
            "cwd": cwd, "tool": tool, "args": args, "autostart": autostart,
        })
        self.sessions[sid] = session
        self._persist()
        self._emit("change", self._public(session))
        return session

    async def remove(self, sid: str) -> bool:
        session = self.sessions.get(sid)
        if not session:
            return False
        if session.get("engine") == "agent":
            proc = session.get("proc")
            if proc:
                try:
                    proc.kill()
                except Exception as err:  # noqa: BLE001
                    log_error("session-manager", err)
            session["proc"] = None
        else:
            try:
                await self.host.forget(sid)
            except Exception as err:  # noqa: BLE001
                log_error("session-manager", err)
            self.host_sessions.pop(sid, None)
        timer = session.get("_busy_timer")
        if timer:
            timer.cancel()
        self._close_log_fh(session)
        self.sessions.pop(sid, None)
        self._persist()
        self._emit("remove", sid)
        return True

    def reorder(self, ids: list) -> bool:
        if not isinstance(ids, list):
            return False
        next_map: dict[str, dict] = {}
        seen = set()
        for raw in ids:
            sid = str(raw)
            if sid in self.sessions and sid not in seen:
                next_map[sid] = self.sessions[sid]
                seen.add(sid)
        for sid, session in self.sessions.items():
            if sid not in seen:
                next_map[sid] = session
        self.sessions = next_map
        self._persist()
        return True

    # --- start / stop ------------------------------------------------------------
    async def start(self, sid: str) -> dict | None:
        session = self.sessions.get(sid)
        if not session:
            return None
        tool = self.config.get("tools", {}).get(session.get("tool"))
        if not tool:
            raise ValueError(f"Unknown tool: {session.get('tool')}")
        if tool.get("engine") == "agent":
            return await self._start_agent(session, tool)
        return await self._start_pty(session, tool)

    async def _start_pty(self, session: dict, tool: dict) -> dict:
        if session.get("state") == "running":
            return session
        session["engine"] = "pty"

        command = resolve_command(tool.get("command"))
        user_args = split_args(session.get("args", ""))
        argv = split_args(tool.get("defaultArgs", "")) + user_args
        user_resume = any(a in RESUME_FLAGS for a in user_args)
        if session.get("tool") == "claude" and not user_resume and has_prior_conversation(session.get("cwd")):
            argv.insert(0, "-c")
        name_flag = tool.get("nameFlag")
        if name_flag and session.get("name") and name_flag not in user_args:
            argv.insert(0, session.get("name"))
            argv.insert(0, name_flag)

        log_path = os.path.join(logs_dir, f"{safe_name(session.get('name'))}-{session['id'][:8]}.log")
        session["log_path"] = log_path
        _append_log(log_path, f"\r\n===== webpty start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\r\n")

        start_opts = {
            "id": session["id"],
            "command": command,
            "args": argv,
            "cwd": session.get("cwd"),
            "cols": session.get("cols") or DEFAULT_COLS,
            "rows": session.get("rows") or DEFAULT_ROWS,
        }

        def fail(err: Exception) -> dict:
            message = f"[webpty] failed to spawn {command}: {err}\r\n"
            _append_log(log_path, message)
            session["state"] = "stopped"
            session["exit_code"] = -1
            self._emit("output", session["id"], message.encode("utf-8"))
            self._emit("change", self._public(session))
            raise err

        try:
            started = await self.host.start(start_opts)
        except Exception as err:  # noqa: BLE001
            message = getattr(err, "message", None) or str(err)
            if message != "already started":
                return fail(err)
            # Host may own this id from a prior run — probe and reattach.
            view = None
            try:
                result = await self.host.list()
                self.host_sessions = {s["id"]: s for s in result.get("sessions", [])}
                view = self.host_sessions.get(session["id"])
            except Exception as err:  # noqa: BLE001
                log_error("session-manager", err)
            if view and view.get("alive"):
                await self._reattach(session, view)
                return session
            try:
                await self.host.forget(session["id"])
            except Exception as err:  # noqa: BLE001
                log_error("session-manager", err)
            self.host_sessions.pop(session["id"], None)
            try:
                started = await self.host.start(start_opts)
            except Exception as err2:  # noqa: BLE001
                return fail(err2)

        session["pid"] = started.get("pid")
        session["mode"] = "pty-host"
        session["started_at"] = int(time.time() * 1000)
        session["state"] = "running"
        session["exit_code"] = None
        session["signal"] = None
        self._mark_busy(session)  # running → tab dot blinks until idle 5s

        try:
            await self.host.attach(session["id"])
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] attach failed for {session['id']}: {err}", flush=True)

        self._emit("change", self._public(session))
        return session

    async def _reattach(self, session: dict, host_view: dict) -> bool:
        if session.get("engine") != "pty":
            return False
        session["pid"] = host_view.get("pid")
        session["mode"] = "pty-host"
        session["started_at"] = host_view.get("started_at") or int(time.time() * 1000)
        session["state"] = "running" if host_view.get("alive") else "stopped"
        session["exit_code"] = host_view.get("exit_code")
        session["signal"] = host_view.get("exit_signal")
        if not session.get("log_path"):
            session["log_path"] = os.path.join(
                logs_dir, f"{safe_name(session.get('name'))}-{session['id'][:8]}.log")
        _append_log(session["log_path"], f"\r\n===== webpty reattach {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\r\n")
        try:
            await self.host.attach(session["id"])
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] reattach failed for {session['id']}: {err}", flush=True)
            return False
        self._emit("change", self._public(session))
        return True

    # --- agent engine --------------------------------------------------------------
    async def _start_agent(self, session: dict, tool: dict) -> dict:
        if session.get("state") == "running":
            return session
        command = resolve_command(tool.get("command"))
        perm_mode = tool.get("permissionMode") or "bypassPermissions"
        argv = [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--permission-mode", perm_mode,
        ]
        resuming = bool(session.get("agent_session_id"))
        if resuming:
            argv += ["--resume", session["agent_session_id"]]
        argv += split_args(session.get("args", ""))

        log_path = os.path.join(logs_dir, f"{safe_name(session.get('name'))}-{session['id'][:8]}.log")
        session["log_path"] = log_path
        _append_log(log_path, f"\r\n===== webpty agent start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\r\n")

        use_shell = os.name == "nt" and command.lower().endswith((".cmd", ".bat"))

        try:
            proc = await asyncio.create_subprocess_exec(
                *([command] if use_shell else [command] + argv),
                cwd=session.get("cwd"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=use_shell,
            )
        except Exception as err:  # noqa: BLE001
            session["state"] = "stopped"
            session["exit_code"] = -1
            session["proc"] = None
            self._push_agent(session, {"t": "error", "message": f"failed to spawn {command}: {err}"})
            self._emit("change", self._public(session))
            raise err

        session["proc"] = proc
        session["pid"] = proc.pid
        session["mode"] = "agent"
        session["engine"] = "agent"
        session["started_at"] = int(time.time() * 1000)
        session["state"] = "running"
        session["exit_code"] = None
        session["signal"] = None
        session["turn_active"] = False

        state = {"got_init": False, "buf": ""}

        async def read_stdout() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                _append_log(log_path, text)
                state["buf"] += text
                while "\n" in state["buf"]:
                    line, _, rest = state["buf"].partition("\n")
                    state["buf"] = rest
                    line = line.strip()
                    if line and self._handle_agent_line(session, line):
                        state["got_init"] = True

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                _append_log(log_path, f"[stderr] {chunk.decode('utf-8', 'replace')}")

        async def wait_exit() -> None:
            code = await proc.wait()
            session["state"] = "stopped"
            session["exit_code"] = code
            session["signal"] = None
            session["proc"] = None
            session["pid"] = None
            session["turn_active"] = False
            _append_log(log_path, f"\r\n[webpty] agent exited code={code}\r\n")
            if resuming and not state["got_init"]:
                session["agent_session_id"] = None
                self._persist()
                self._push_agent(session, {
                    "t": "error",
                    "message": "previous conversation could not be resumed — start a new message to begin fresh",
                })
            else:
                self._push_agent(session, {"t": "exit", "code": code})
            self._emit("change", self._public(session))
            self._emit("session_event", {
                "type": "crashed" if session.get("signal") else
                        ("completed" if session.get("exit_code") == 0 else "failed"),
                "session_id": session["id"], "name": session.get("name"),
                "tool": session.get("tool"), "project": session.get("cwd"),
                "state": "stopped", "exit_code": session.get("exit_code"),
                "signal": session.get("signal"), "ts": time.time(),
            })

        session["_tasks"] = [
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
            asyncio.create_task(wait_exit()),
        ]
        self._emit("change", self._public(session))
        return session

    def _handle_agent_line(self, session: dict, line: str) -> bool:
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(evt, dict):
            return False
        etype = evt.get("type")

        if etype == "system":
            if evt.get("subtype") == "init":
                sid = evt.get("session_id")
                if sid and sid != session.get("agent_session_id"):
                    session["agent_session_id"] = sid
                    self._persist()
                self._push_agent(session, {
                    "t": "system", "model": evt.get("model"), "cwd": evt.get("cwd"),
                    "permissionMode": evt.get("permissionMode"), "sessionId": sid,
                })
                return True
            return False

        if etype == "assistant":
            blocks = (evt.get("message") or {}).get("content") or []
            mid = (evt.get("message") or {}).get("id")
            for block in blocks:
                if block.get("type") == "text":
                    self._push_agent(session, {"t": "text", "id": mid, "text": block.get("text") or ""})
                elif block.get("type") == "thinking":
                    self._push_agent(session, {"t": "thinking", "id": mid, "text": block.get("thinking") or ""})
                elif block.get("type") == "tool_use":
                    self._push_agent(session, {
                        "t": "tool_use", "id": mid, "toolId": block.get("id"),
                        "name": block.get("name"), "input": block.get("input"),
                    })
            return False

        if etype == "user":
            blocks = (evt.get("message") or {}).get("content") or []
            for block in blocks:
                if block.get("type") == "tool_result":
                    self._push_agent(session, {
                        "t": "tool_result", "toolId": block.get("tool_use_id"),
                        "content": normalize_tool_result(block.get("content")),
                        "isError": bool(block.get("is_error")),
                    })
            return False

        if etype == "result":
            session["turn_active"] = False
            if evt.get("session_id"):
                session["agent_session_id"] = evt.get("session_id")
            self._push_agent(session, {
                "t": "result", "isError": bool(evt.get("is_error")),
                "costUsd": evt.get("total_cost_usd"), "durationMs": evt.get("duration_ms"),
                "numTurns": evt.get("num_turns"),
                "text": None if evt.get("subtype") == "success" else (evt.get("result") or evt.get("subtype") or "error"),
            })
            self._emit("change", self._public(session))
            return False

        # Lines that carry usage but no transcript item (message_start /
        # message_delta / ...) are re-emitted verbatim so business-layer
        # listeners (CostTracker) can meter them in realtime.
        usage = evt.get("usage")
        if isinstance(usage, dict) and usage:
            self._emit("agentEvent", session["id"], {
                "type": "usage", "raw": line, "tool": session.get("tool")})
        return False

    def _push_agent(self, session: dict, item: dict) -> None:
        if "transcript" not in session:
            session["transcript"] = []
        if item.get("t") != "user":
            session["last_output_at"] = int(time.time() * 1000)
        session["transcript"].append(item)
        if len(session["transcript"]) > AGENT_MAX_ITEMS:
            del session["transcript"][:len(session["transcript"]) - AGENT_MAX_ITEMS]
        self._emit("agentEvent", session["id"], item)

    def agent_send(self, sid: str, text) -> bool:  # type: ignore[no-untyped-def]
        session = self.sessions.get(sid)
        if not session or session.get("engine") != "agent":
            return False
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        message = str(text)
        if not message.strip():
            return False
        proc = session.get("proc")
        if not proc or session.get("state") != "running" or proc.stdin is None:
            return False
        try:
            self._push_agent(session, {"t": "user", "text": message})
            session["turn_active"] = True
            payload = json.dumps({"type": "user", "message": {"role": "user", "content": message}}) + "\n"
            proc.stdin.write(payload.encode("utf-8"))
            self._emit("change", self._public(session))
            return True
        except Exception as err:  # noqa: BLE001
            log_error("session-manager", err)
            return False

    def transcript(self, sid: str) -> list:
        session = self.sessions.get(sid)
        return session.get("transcript", []) if session else []

    async def stop(self, sid: str) -> bool:
        session = self.sessions.get(sid)
        if not session:
            return False
        if session.get("engine") == "agent":
            proc = session.get("proc")
            if proc:
                try:
                    proc.kill()
                except Exception as err:  # noqa: BLE001
                    log_error("session-manager", err)
                session["proc"] = None
        else:
            exited_naturally = False
            if session.get("state") == "running":
                waiter = self._wait_host_exit(sid, 600)
                self.host.input(sid, "\x03\x03exit\r")
                exited_naturally = await waiter
            if not exited_naturally:
                try:
                    await self.host.kill(sid)
                except Exception as err:  # noqa: BLE001
                    log_error("session-manager", err)
        session["state"] = "stopped"
        session["pid"] = None
        timer = session.get("_busy_timer")
        if timer:
            timer.cancel()
        session["busy"] = False
        self._close_log_fh(session)
        self._emit("change", self._public(session))
        self._emit("session_event", {
            "type": "terminated", "session_id": session["id"], "name": session.get("name"),
            "tool": session.get("tool"), "project": session.get("cwd"),
            "state": "stopped", "exit_code": session.get("exit_code"),
            "signal": session.get("signal"), "ts": time.time(),
        })
        return True

    async def _wait_host_exit(self, sid: str, ms: int) -> bool:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        done = {"flag": False}

        def on_exit(exited_id, _code, _signal) -> None:  # type: ignore[no-untyped-def]
            if exited_id == sid and not done["flag"]:
                done["flag"] = True
                self.host.off("exit", on_exit)
                if not fut.done():
                    fut.set_result(True)

        self.host.on("exit", on_exit)
        try:
            return await asyncio.wait_for(fut, timeout=ms / 1000)
        except asyncio.TimeoutError:
            done["flag"] = True
            self.host.off("exit", on_exit)
            return False

    def write(self, sid: str, bytes_) -> bool:  # type: ignore[no-untyped-def]
        session = self.sessions.get(sid)
        if not session or session.get("state") != "running":
            return False
        if session.get("engine") == "agent":
            return False
        if isinstance(bytes_, bytes):
            data = bytes_
        else:
            data = str(bytes_).encode("utf-8")
        return self.host.input(sid, data)

    def resize(self, sid: str, cols: int, rows: int) -> bool:
        session = self.sessions.get(sid)
        if not session:
            return False
        session["cols"] = cols
        session["rows"] = rows
        if session.get("engine") == "agent" or session.get("state") != "running":
            return True
        self.host.resize(sid, cols, rows)
        return True

    async def autostart(self) -> None:
        for session in list(self.sessions.values()):
            tool = self.config.get("tools", {}).get(session.get("tool"))
            engine = (tool or {}).get("engine", "pty")
            try:
                if engine == "agent":
                    if session.get("autostart"):
                        await self.start(session["id"])
                    continue
                host_view = self.host_sessions.get(session["id"])
                if host_view:
                    if await self._reattach(session, host_view):
                        continue
                if not session.get("autostart"):
                    continue
                await self.start(session["id"])
            except Exception as err:  # noqa: BLE001
                print(f"autostart {session.get('name')} failed: {err}", flush=True)

    # --- host event handlers ---------------------------------------------------------
    def _on_host_output(self, sid: str, chunk: bytes) -> None:
        session = self.sessions.get(sid)
        if not session:
            return
        self._emit_output(session, chunk)

    def _on_host_exit(self, sid: str, code, signal_) -> None:  # type: ignore[no-untyped-def]
        session = self.sessions.get(sid)
        if not session:
            return
        session["state"] = "stopped"
        session["exit_code"] = code
        session["signal"] = signal_
        session["pid"] = None
        timer = session.get("_busy_timer")
        if timer:
            timer.cancel()
        session["busy"] = False
        self._close_log_fh(session)
        if session.get("log_path"):
            _append_log(session["log_path"], f"\r\n[webpty] exited code={code} signal={signal_}\r\n")
        self._emit("change", self._public(session))
        self._emit("session_event", {
            "type": "crashed" if session.get("signal") else
                    ("completed" if session.get("exit_code") == 0 else "failed"),
            "session_id": session["id"], "name": session.get("name"),
            "tool": session.get("tool"), "project": session.get("cwd"),
            "state": "stopped", "exit_code": session.get("exit_code"),
            "signal": session.get("signal"), "ts": time.time(),
        })

    def _on_host_disconnect(self) -> None:
        self.host_ready = False
        print("[webpty] disconnected from pty-host — monitor will reconnect", flush=True)
        # Do NOT mark running sessions stopped here: pty-host may have crashed
        # while the underlying processes are still alive. The host monitor
        # reconnects and re-attaches them (or marks genuinely dead ones
        # stopped), so keep state pending until then.

    # --- host monitor ------------------------------------------------------------
    def start_host_monitor(self, interval_s: float = 2.0) -> None:
        self.stop_host_monitor()
        self._monitor_task = asyncio.get_event_loop().create_task(
            self._monitor_loop(interval_s))

    def stop_host_monitor(self) -> None:
        if getattr(self, "_monitor_task", None):
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _monitor_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                # Reconnect when the socket is down OR the host has not been
                # confirmed ready (list after reconnect may have failed). Stub
                # hosts without a `connected` attr fall back to host_ready only.
                if not (getattr(self.host, "connected", True) and self.host_ready):
                    await self._reconnect_host()
            except Exception as err:  # noqa: BLE001
                print(f"[webpty] host monitor error: {err}", flush=True)

    async def _reconnect_host(self) -> None:
        print("[webpty] reconnecting to pty-host...", flush=True)
        await self.host.connect()
        try:
            result = await self.host.list()
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] host list after reconnect failed: {err}", flush=True)
            # Keep host_ready False and skip reconciliation so the monitor
            # retries — stale host_sessions must not mark alive sessions dead.
            return
        self.host_sessions = {s["id"]: s for s in result.get("sessions", [])}
        self.host_ready = True
        # Iterate over a snapshot: _reattach can mutate self.sessions via
        # event callbacks, which raised "dictionary changed size during
        # iteration" inside the monitor loop.
        for sid, session in list(self.sessions.items()):
            if session.get("engine") != "pty" or session.get("state") != "running":
                continue
            view = self.host_sessions.get(sid)
            if view and view.get("alive"):
                await self._reattach(session, view)
            else:
                session["state"] = "stopped"
                session["pid"] = None
                self._emit("change", self._public(session))
        self._emit("reconnected")

    # --- helpers -------------------------------------------------------------------------
    def _inflate(self, stored: dict) -> dict:
        tool = self.config.get("tools", {}).get(stored.get("tool")) or {}
        engine = tool.get("engine", "pty")
        return {
            "id": stored.get("id") or str(uuid.uuid4()),
            "name": stored.get("name"),
            "cwd": stored.get("cwd"),
            "tool": stored.get("tool"),
            "args": stored.get("args") or "",
            "autostart": bool(stored.get("autostart")),
            "state": "stopped",
            "pid": None,
            "proc": None,
            "started_at": None,
            "exit_code": None,
            "signal": None,
            "log_path": stored.get("logPath") or stored.get("log_path"),
            "mode": None,
            "cols": DEFAULT_COLS,
            "rows": DEFAULT_ROWS,
            "engine": engine,
            "agent_session_id": stored.get("agentSessionId") or stored.get("agent_session_id"),
            "transcript": [],
            "turn_active": False,
            "busy": False,
            "last_output_at": None,
            "recent_buf": RingBuffer(RECENT_BUF_CAP) if engine == "pty" else None,
        }

    def _persist(self) -> None:
        self.config["sessions"] = [
            {
                "id": s["id"], "name": s.get("name"), "cwd": s.get("cwd"),
                "tool": s.get("tool"), "args": s.get("args"), "autostart": s.get("autostart"),
                "logPath": s.get("log_path"),
                "agentSessionId": s.get("agent_session_id"),
            }
            for s in self.sessions.values()
        ]
        self.save()

    def _public(self, session: dict) -> dict:
        return {
            "id": session["id"],
            "name": session.get("name"),
            "cwd": session.get("cwd"),
            "tool": session.get("tool"),
            "args": session.get("args"),
            "autostart": session.get("autostart"),
            "state": session.get("state"),
            "pid": session.get("pid"),
            "started_at": session.get("started_at"),
            "exit_code": session.get("exit_code"),
            "signal": session.get("signal"),
            "logPath": session.get("log_path"),
            "mode": session.get("mode"),
            "engine": session.get("engine") or "pty",
            "turnActive": bool(session.get("turn_active")),
            "busy": bool(session.get("turn_active")) if session.get("engine") == "agent" else bool(session.get("busy")),
            "lastOutputAt": session.get("last_output_at"),
        }

    def _emit_output(self, session: dict, chunk: bytes) -> None:
        session["last_output_at"] = int(time.time() * 1000)
        self._mark_busy(session)
        if session.get("recent_buf"):
            session["recent_buf"].push(chunk)
        if session.get("log_path"):
            self._append_log_cached(session, chunk)
        self._emit("output", session["id"], chunk)

    # Cached per-session log file handle: opening/closing the log on every
    # output chunk (~60/s on an active terminal) was pure syscall overhead.
    def _append_log_cached(self, session: dict, chunk: bytes) -> None:
        try:
            fh = session.get("_log_fh")
            if fh is None:
                fh = open(session["log_path"], "ab", buffering=0)
                session["_log_fh"] = fh
            fh.write(chunk)
        except OSError:
            pass

    def _close_log_fh(self, session: dict) -> None:
        fh = session.pop("_log_fh", None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _mark_busy(self, session: dict) -> None:
        if not session.get("busy"):
            session["busy"] = True
            self._emit("change", self._public(session))
        timer = session.get("_busy_timer")
        if timer and not timer.done():
            timer.cancel()
        elif timer:
            # previous timer finished but session still busy — reuse slot
            pass

        async def _clear() -> None:
            await asyncio.sleep(BUSY_IDLE_MS / 1000)
            session["busy"] = False
            self._emit("change", self._public(session))

        session["_busy_timer"] = asyncio.get_event_loop().create_task(_clear())
