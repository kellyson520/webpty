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
import signal
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
MAX_AGENT_BUF = 2 * 1024 * 1024  # audit M6: partial-line cap per session
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


def _reasonix_has_history(cwd: str) -> bool:
    """True when reasonix has persisted sessions FOR THIS PROJECT DIRECTORY.

    reasonix stores sessions per working directory under
    ~/.reasonix/projects/<encoded-cwd>/sessions/ — encoding is
    '/root' → '-root', '/root/webpty' → '-root-webpty'. Checking the
    project-scoped dir (not the global store) means `-c` resumes the
    sessions of THIS project, never the ones held by reasonix serve
    (which runs in a different cwd and locks its own directory).
    """
    import glob

    enc = "-" + str(cwd).replace("/", "-").lstrip("-")
    base = os.path.join(os.path.expanduser("~"), ".reasonix",
                        "projects", enc, "sessions")
    return bool(glob.glob(os.path.join(base, "*.jsonl")))


def _append_log(log_path: str, text: str) -> None:
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # Rotate at 5MB (Issue 3 hardening): rename to .1, keep newest only.
        try:
            if os.path.getsize(log_path) > 5 * 1024 * 1024:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
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
        # autostart 会话非 0 退出自动重启(带退避);挂起检测去重
        self._restart_counts: dict[str, int] = {}
        self._restart_config = config.get("restart") or {}
        self._stall_reported: dict[str, float] = {}
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
        self.host.on("dropped", self._on_host_dropped)
        self.host.on("replay", self._on_host_replay)
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

    def list(self, limit: int | None = None) -> list[dict]:
        """Session summaries (audit 7.1): args/logPath can be large; the
        polling list only needs state/name/tool/busy. Full detail via
        public(sid)."""
        items = [self._public(s) for s in self.sessions.values()]
        if limit is not None and limit > 0:
            items = items[:limit]
        return items

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

    def tail_log(self, sid: str, max_bytes: int = 128 * 1024) -> bytes | None:
        """Audit S2: after a server+pty-host restart the in-memory ring
        buffer is empty and the terminal would come up blank — recover the
        tail from disk (log + rotated .1), trimming on a UTF-8 boundary."""
        s = self.sessions.get(sid)
        if not s or not s.get("log_path"):
            return None
        parts: list[bytes] = []
        total = 0
        for p in (s["log_path"] + ".1", s["log_path"]):
            try:
                size = os.path.getsize(p)
                read = min(size, max_bytes - total)
                if read <= 0:
                    continue
                with open(p, "rb") as f:
                    f.seek(size - read)
                    parts.append(f.read(read))
                    total += read
            except OSError:
                continue
        if not parts:
            return None
        data = b"".join(parts)
        # Trim to a UTF-8 boundary (avoid a trailing partial char).
        while data and data[-1] & 0xC0 == 0x80:
            data = data[:-1]
        if data and data[-1] & 0x80:
            data = data[:-1]
        return data

    def create(self, *, name: str, cwd: str, tool: str, args: str = "",
               autostart: bool = False,
               permissionMode: str | None = None) -> dict:
        sid = str(uuid.uuid4())
        session = self._inflate({
            "id": sid, "name": name or os.path.basename(cwd) or cwd,
            "cwd": cwd, "tool": tool, "args": args, "autostart": autostart,
            "permissionMode": permissionMode,
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
            # Audit H3: cancel the reader/waiter tasks — wait_exit() would
            # otherwise emit a ghost failed event after the session is gone.
            for task in session.get("_tasks", ()):
                task.cancel()
        else:
            try:
                await self.host.forget(sid)
            except Exception as err:  # noqa: BLE001
                log_error("session-manager", err)
            self.host_sessions.pop(sid, None)
        timer = session.get("_busy_handle")
        if timer:
            timer.cancel()
        self._close_log_fh(session)
        # Audit M1: a removed session's log files (up to ~10MB each) would
        # otherwise linger on disk forever — delete them with the session.
        log_path = session.get("log_path")
        if log_path:
            for p in (log_path, log_path + ".1"):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.sessions.pop(sid, None)
        self._restart_counts.pop(sid, None)  # id reuse must not inherit counts
        self._stall_reported.pop(sid, None)
        self._persist()
        self._emit("remove", sid)
        self._emit("session_event", {
            "type": "removed", "session_id": sid,
            "name": session.get("name"), "tool": session.get("tool"),
            "project": session.get("cwd"), "state": "removed",
            "exit_code": None, "signal": None, "ts": time.time(),
        })
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
        lock = session.get("_lock")
        if lock is not None:
            async with lock:
                return await self._start_locked(sid)
        return await self._start_locked(sid)

    async def _start_locked(self, sid: str) -> dict | None:
        session = self.sessions.get(sid)
        if not session:
            return None
        # Audit H2: a fresh start clears the user-stop marker so the compat
        # retry (old claude + --include-partial-messages) works again.
        session["_user_stopped"] = False
        tool = self.config.get("tools", {}).get(session.get("tool"))
        if not tool:
            raise ValueError(f"Unknown tool: {session.get('tool')}")
        if tool.get("engine") == "agent":
            return await self._start_agent(session, tool)
        return await self._start_pty(session, tool)

    async def _start_pty_retry_copy(self, session: dict) -> None:
        """Restart a reasonix-family session WITHOUT -c after an in-use hang.

        Called from _emit_output when the terminal printed 'session is in
        use' (user passed -c explicitly while another process holds the
        global session lock). A plain start never conflicts — resume stays
        available to the user via session args later.
        """
        tool = self.config.get("tools", {}).get(session.get("tool")) or {}
        command = resolve_command(tool.get("command"))
        user_args = split_args(session.get("args", ""))
        # Strip -c/--copy so the retry is a plain, lock-free start.
        base_args = [a for a in split_args(tool.get("defaultArgs", "")) + user_args
                     if a not in ("-c", "--continue", "--copy")]
        argv = list(base_args)
        name_flag = tool.get("nameFlag")
        if name_flag and session.get("name") and name_flag not in user_args:
            argv.append(name_flag)
            argv.append(session.get("name"))
        log_path = session.get("log_path") or os.path.join(
            logs_dir, f"{safe_name(session.get('name'))}-{session['id'][:8]}.log")
        session["log_path"] = log_path
        _append_log(log_path, f"\r\n===== webpty auto-restart (plain) {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\r\n")
        session["state"] = "running"
        try:
            started = await self.host.start({
                "id": session["id"],
                "command": command,
                "args": argv,
                "cwd": session.get("cwd"),
                "cols": session.get("cols") or DEFAULT_COLS,
                "rows": session.get("rows") or DEFAULT_ROWS,
            })
            session["pid"] = started.get("pid")
            session["started_at"] = int(time.time() * 1000)
            session["mode"] = "pty-host"
            self._emit("change", self._public(session))
        except Exception as err:  # noqa: BLE001
            session["state"] = "stopped"
            session["exit_code"] = -1
            session["last_error"] = str(err)[:200]
            _append_log(log_path, f"[webpty] auto-resume failed: {err}\r\n")
            self._emit("change", self._public(session))

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
        # reasonix-family: do NOT auto-add -c. reasonix keeps a GLOBAL session
        # lock (one active session at a time); 'reasonix -c' tries to resume
        # the locked session and errors 'session is in use' (then hangs).
        # A plain 'reasonix' start never conflicts (verified) — users who
        # want to continue explicitly pass -c/--continue in session args.
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
            # reasonix holds a GLOBAL session lock (one active session at a
            # time). When another process (reasonix serve, or another webpty
            # session of the same project) holds it, retry with
            # `-c --copy` — --copy requires --continue/--resume and opens a
            # duplicated conversation instead of failing.
            if (session.get("tool") in ("reasonix", "opencode")
                    and ("in use" in message or "already in use" in message)):
                # reasonix holds a GLOBAL session lock (one active session at
                # a time). Another process (reasonix serve, another webpty
                # session) holds it → retry with `-c --copy`. --copy requires
                # --continue/--resume and opens a duplicated conversation
                # (verified: works even in a fresh project dir).
                retry_argv = list(argv)
                if "--copy" not in retry_argv:
                    retry_argv.insert(0, "--copy")
                if "-c" not in retry_argv:
                    retry_argv.insert(0, "-c")
                start_opts["args"] = retry_argv
                try:
                    started = await self.host.start(start_opts)
                    _append_log(log_path,
                                "[webpty] reasonix session in use — resumed with --copy\r\n")
                except Exception as err2:  # noqa: BLE001
                    return fail(err2)
            elif message != "already started":
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
        # Audit 2.1: per-session permission mode overrides the tool default
        # (created via the new-session form / session API).
        perm_mode = (session.get("permissionMode")
                     or tool.get("permissionMode") or "bypassPermissions")
        argv = [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--permission-mode", perm_mode,
        ]
        # Token-level streaming (audit V4): without this flag claude emits
        # whole text blocks only; with it the same block arrives in partial
        # chunks which the S3 delta-dedup turns into a live typewriter
        # effect. Old claude versions exit on unknown flags — the
        # _start_agent retry in _spawn_failed drops it once.
        if session.get("tool") == "claude" and not session.get("_partial_off"):
            argv.append("--include-partial-messages")
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
            import codecs
            assert proc.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                _append_log(log_path, text)
                state["buf"] += text
                # Audit M6: cap the partial-line buffer — a runaway agent
                # printing an endless unterminated line must not grow memory
                # without bound. Truncate from the front (keep the tail, the
                # most likely to be the current partial JSON).
                if len(state["buf"]) > MAX_AGENT_BUF:
                    state["buf"] = state["buf"][-MAX_AGENT_BUF:]
                while "\n" in state["buf"]:
                    line, _, rest = state["buf"].partition("\n")
                    state["buf"] = rest
                    line = line.strip()
                    if line and self._handle_agent_line(session, line):
                        state["got_init"] = True
            # flush any partial multi-byte character held in the decoder
            tail = decoder.decode(b"", final=True)
            if tail:
                _append_log(log_path, tail)
                state["buf"] += tail

        async def read_stderr() -> None:
            import codecs
            assert proc.stderr is not None
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                chunk = await proc.stderr.read(16384)
                if not chunk:
                    break
                _append_log(log_path, f"[stderr] {decoder.decode(chunk)}")

        async def wait_exit() -> None:
            code = await proc.wait()
            # Audit H3: if the session was removed (or stopped) while the
            # process was running, don't emit a ghost failed/crashed event
            # (it would land in the notification center and possibly email).
            if self.sessions.get(session["id"]) is not session:
                return
            session["state"] = "stopped"
            session["exit_code"] = code
            session["signal"] = None
            session["proc"] = None
            session["pid"] = None
            session["turn_active"] = False
            _append_log(log_path, f"\r\n[webpty] agent exited code={code}\r\n")
            # Compatibility downgrade (audit V4): old claude versions exit
            # on --include-partial-messages. Retry once without the flag.
            # Audit H2: never auto-restart when the user stopped/interrupted
            # the session (SIGKILL/SIGINT also exit non-zero).
            if (code != 0 and session.get("tool") == "claude"
                    and session.get("_partial_off") is not True
                    and not resuming
                    and not session.get("_user_stopped")):
                _append_log(log_path,
                            "[webpty] retrying without --include-partial-messages\r\n")
                session["_partial_off"] = True
                session["state"] = "idle"
                self._emit("change", self._public(session))
                await self.start(session["id"])
                return
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
                # Audit H3: a user-initiated stop is not a crash/failure.
                "type": "stopped" if session.get("_user_stopped") else
                        ("crashed" if session.get("signal") else
                         ("completed" if session.get("exit_code") == 0 else "failed")),
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
        # Audit M6: a multi-MB JSON line would block the event loop in
        # json.loads — refuse absurd lines instead of parsing them.
        if len(line) > MAX_AGENT_BUF:
            log_error("session-manager",
                      f"oversized agent line ({len(line)} bytes) dropped "
                      f"for session {session.get('id')}")
            return False
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
                    text = block.get("text") or ""
                    # Incremental dedup (audit S3): claude may emit the same
                    # message id again with a longer/equal text (retry or
                    # chunked stream). Pushing the FULL text each time makes
                    # the frontend append duplicates. Only push the delta
                    # past the previous text for this id; reset on shorter.
                    prev = session.get("_agent_text", {}).get(mid, "")
                    if text.startswith(prev) and text != prev:
                        delta = text[len(prev):]
                        session.setdefault("_agent_text", {})[mid] = text
                        self._push_agent(session, {"t": "text", "id": mid, "text": delta})
                    elif text != prev:
                        session.setdefault("_agent_text", {})[mid] = text
                        self._push_agent(session, {"t": "text", "id": mid, "text": text})
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
                # Audit B: forward model/project so actual rows group by
                # model and project instead of falling back to tool name.
                "model": evt.get("model"), "project": session.get("cwd"),
                "text": None if evt.get("subtype") == "success" else (evt.get("result") or evt.get("subtype") or "error"),
            })
            self._emit("change", self._public(session))
            # Fall through to the usage forwarder below — result events also
            # carry usage (audit H2) which must not be dropped; when the
            # tool omits total_cost_usd, CostTracker's estimate then has
            # real token counts to work with.

        # Lines that carry usage but no transcript item (message_start /
        # message_delta / stats / ...) are re-emitted verbatim so
        # business-layer listeners (CostTracker) can meter them in realtime.
        # (Audit H1: usage also arrives nested in message.usage — claude
        # message_start — or as flat stats/usage_event lines — reasonix /
        # codex; all three shapes were missing and their token counts
        # silently dropped to zero in realtime.)
        usage = evt.get("usage")
        if not (isinstance(usage, dict) and usage):
            msg_usage = (evt.get("message") or {}).get("usage")
            if isinstance(msg_usage, dict) and msg_usage:
                usage = msg_usage
        if isinstance(usage, dict) and usage:
            self._emit("agentEvent", session["id"], {
                "type": "usage", "raw": line, "tool": session.get("tool")})
        elif evt.get("type") in ("stats", "usage_event"):
            self._emit("agentEvent", session["id"], {
                "type": "usage", "raw": line, "tool": session.get("tool")})
        return False

    def _push_agent(self, session: dict, item: dict) -> None:
        if "transcript" not in session:
            session["transcript"] = []
        if item.get("t") != "user":
            session["last_output_at"] = int(time.time() * 1000)
        # Partial-stream merge (audit V4): token-level deltas for the same
        # message id arrive in many chunks — replace the previous entry for
        # (t=text, id) instead of appending, so AGENT_MAX_ITEMS isn't
        # exhausted by one streaming message and the transcript stays
        # canonical (final text per mid).
        if item.get("t") == "text" and item.get("id"):
            tr = session["transcript"]
            if tr and tr[-1].get("t") == "text" and tr[-1].get("id") == item.get("id"):
                tr[-1] = item
            else:
                tr.append(item)
        else:
            session["transcript"].append(item)
        if len(session["transcript"]) > AGENT_MAX_ITEMS:
            del session["transcript"][:len(session["transcript"]) - AGENT_MAX_ITEMS]
        # Audit T1: persist the transcript incrementally (JSONL) so a server
        # restart doesn't wipe the chat history — the WS snapshot is built
        # from memory only today.
        try:
            tpath = session.get("_transcript_path")
            if tpath is None:
                base_dir = os.path.dirname(session.get("log_path") or "") or logs_dir
                tpath = os.path.join(base_dir, f"{session['id']}.transcript.jsonl")
                session["_transcript_path"] = tpath
                os.makedirs(os.path.dirname(tpath), exist_ok=True)
            with open(tpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass
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
        # Audit A2: reject messages while a turn is active — two tabs
        # sending into one agent process would interleave turns.
        if session.get("turn_active"):
            self._push_agent(session, {
                "t": "system",
                "text": "（上一回合仍在进行，等待完成或点 ■ 停止）",
            })
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
        lock = session.get("_lock")
        if lock is not None:
            async with lock:
                return await self._stop_locked(sid)
        return await self._stop_locked(sid)

    async def interrupt(self, sid: str) -> bool:
        """Audit C1: interrupt the current turn — agent sessions get
        SIGINT (claude aborts the turn and saves its session checkpoint,
        so --resume keeps working); after a 3s grace it escalates to
        SIGKILL. PTY sessions just get Ctrl+C."""
        session = self.sessions.get(sid)
        if not session:
            return False
        if session.get("engine") == "agent":
            # Audit H2: SIGINT exits non-zero — don't let wait_exit mistake
            # it for a launch failure and auto-restart the session.
            session["_user_stopped"] = True
            proc = session.get("proc")
            if not proc or proc.returncode is not None:
                return False
            try:
                proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                return False
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            return True
        # PTY path: send Ctrl+C into the terminal.
        self.host.input(sid, "\x03")
        return True

    async def reset(self, sid: str) -> bool:
        """Audit A1: start a brand-new conversation for an agent session —
        clears agent_session_id so the next start() runs without --resume
        (the old transcript stays in the UI until a reload)."""
        session = self.sessions.get(sid)
        if not session:
            return False
        if session.get("engine") != "agent":
            return False
        session["agent_session_id"] = None
        self._persist()
        self._emit("change", self._public(session))
        return True

    async def _stop_locked(self, sid: str) -> bool:
        session = self.sessions.get(sid)
        if not session:
            return False
        # Audit H2: mark user-initiated stops so wait_exit never auto-restarts.
        session["_user_stopped"] = True
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
        timer = session.get("_busy_handle")
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
        # Audit L2: clamp hostile values (NaN / negatives / absurd sizes)
        # so session state can't be poisoned by a malformed client.
        try:
            cols = max(1, min(int(cols), 1000))
            rows = max(1, min(int(rows), 1000))
        except (TypeError, ValueError):
            cols, rows = 120, 30
        session["cols"] = cols
        session["rows"] = rows
        if session.get("engine") == "agent" or session.get("state") != "running":
            return True
        self.host.resize(sid, cols, rows)
        return True

    async def autostart(self) -> None:
        # reasonix-family CLIs hold a global session lock — starting them in
        # parallel would trip "session is in use"; everything else can boot
        # concurrently (audit F4b: N serial host.start RTTs → ~1 RTT).
        serial_tools = {"reasonix", "opencode"}
        fast: list[str] = []
        for session in list(self.sessions.values()):
            tool = self.config.get("tools", {}).get(session.get("tool"))
            engine = (tool or {}).get("engine", "pty")
            try:
                if engine == "agent":
                    if session.get("autostart"):
                        fast.append(session["id"])
                    continue
                host_view = self.host_sessions.get(session["id"])
                if host_view:
                    if await self._reattach(session, host_view):
                        continue
                if not session.get("autostart"):
                    continue
                if session.get("tool") in serial_tools:
                    await self.start(session["id"])
                else:
                    fast.append(session["id"])
            except Exception as err:  # noqa: BLE001
                print(f"autostart {session.get('name')} failed: {err}", flush=True)
                # Audit C3: surface the failure (tab tooltip) + one fast
                # retry for pty sessions (transient host races).
                session["last_error"] = str(err)[:200]
                self._emit("change", self._public(session))
                if engine != "agent":
                    await asyncio.sleep(2.0)
                    try:
                        await self.start(session["id"])
                    except Exception as err2:  # noqa: BLE001
                        print(f"autostart retry {session.get('name')} failed: {err2}", flush=True)
        if fast:
            results = await asyncio.gather(
                *(self.start(sid) for sid in fast), return_exceptions=True)
            for sid, res in zip(fast, results):
                if isinstance(res, Exception):
                    print(f"autostart {sid} failed: {res}", flush=True)
                    session = self.sessions.get(sid)
                    if session:
                        session["last_error"] = str(res)[:200]
                        self._emit("change", self._public(session))

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
        # reasonix auto-resume: the process can exit code 1 right after
        # start when (a) the GLOBAL session lock is held by another process
        # ("session is in use") or (b) -c was added but there is no
        # resumable session ("没有可恢复的会话"). In both cases restart once
        # with `-c --copy` — it always works (verified even in a fresh
        # project dir) and gives the user a usable session instead of a
        # dead one.
        log_text = ""
        if session.get("log_path"):
            try:
                with open(session["log_path"], "rb") as f:
                    # Tail-read only (audit L1): the full log of a long
                    # session could be tens of MB — reading it all on every
                    # exit just to match "in use" was a memory spike.
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 65536))
                    log_text = f.read().decode("utf-8", errors="replace")
            except OSError:
                log_text = ""
        tool = session.get("tool")
        is_rx = tool in ("reasonix", "opencode")
        retryable = (code != 0 and is_rx
                     and not session.get("_resume_retried")
                     and ("in use" in log_text
                          or "没有可恢复的会话" in log_text))
        if retryable:
            session["_resume_retried"] = True
            try:
                self._start_pty_retry_copy(session)
                return  # restart in flight; don't mark stopped yet
            except Exception:  # noqa: BLE001
                pass  # fall through to normal exit handling

        session["state"] = "stopped"
        session["exit_code"] = code
        session["signal"] = signal_
        session["pid"] = None
        timer = session.get("_busy_handle")
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
        # Generic auto-restart: autostart sessions that exit non-zero (and
        # weren't already handled by the reasonix in-use retry) are restarted
        # with backoff, up to max_restarts. Manual stop (exit_code None via
        # stop()) never restarts.
        if (code not in (0, None) and session.get("autostart")
                and not session.get("_resume_retried")):
            self._maybe_restart(session, code)

    def _maybe_restart(self, session: dict, code) -> None:  # type: ignore[no-untyped-def]
        key = session["id"]
        max_restarts = int(self._restart_config.get("max_restarts", 3))
        backoff = float(self._restart_config.get("backoff_s", 10))
        n = self._restart_counts.get(key, 0) + 1
        if n > max_restarts:
            self._restart_counts.pop(key, None)
            self._emit("session_event", {
                "type": "failed", "session_id": key,
                "name": session.get("name"), "tool": session.get("tool"),
                "project": session.get("cwd"), "state": "stopped",
                "exit_code": code, "signal": None, "ts": time.time(),
                "restart_exhausted": True,
            })
            return
        self._restart_counts[key] = n
        self._stall_reported.pop(key, None)  # fresh run → re-arm stall watch
        loop = asyncio.get_event_loop()
        # Exponential backoff (audit S1): backoff, 2x, 4x, ... — a fixed 10s
        # burned all 3 retries in 30s against a transient failure (reasonix
        # global lock, API rate limit). 10/30/90s covers a wider window.
        wait = backoff * (2 ** (n - 1))
        loop.call_later(wait, lambda: asyncio.create_task(self.start(key)))

    def _on_host_disconnect(self) -> None:
        self.host_ready = False
        print("[webpty] disconnected from pty-host — monitor will reconnect", flush=True)
        # Do NOT mark running sessions stopped here: pty-host may have crashed
        # while the underlying processes are still alive. The host monitor
        # reconnects and re-attaches them (or marks genuinely dead ones
        # stopped), so keep state pending until then.

    def _on_host_dropped(self, sid: str) -> None:
        """pty-host's send buffer overflowed for our connection — it dropped
        the pipe and asked us to resync. The next output for this session
        must be preceded by a full snapshot (frontend already handles the
        resync frame)."""
        self._emit("resync", sid)

    def _on_host_replay(self, sid: str, chunk: bytes) -> None:
        """A reattach replayed the host's ring buffer: this is a FULL-state
        snapshot, not incremental output. Consumers (server _ws_session)
        forward it as a resync frame so the frontend wipes and replays —
        appending it as ordinary output would double-render the TUI after
        every pty-host reconnect."""
        self._emit("resync", sid, chunk)

    # --- host monitor ------------------------------------------------------------
    def start_host_monitor(self, interval_s: float = 2.0) -> None:
        self.stop_host_monitor()
        self._monitor_task = asyncio.get_event_loop().create_task(
            self._monitor_loop(interval_s))

    def stop_host_monitor(self) -> None:
        if getattr(self, "_monitor_task", None):
            self._monitor_task.cancel()
            self._monitor_task = None

    async def stop_host(self) -> None:
        """Audit M5: close the pty-host client connection (reader task +
        socket) so shutdown leaves nothing dangling."""
        try:
            await self.host.close()
        except Exception as err:  # noqa: BLE001
            log_error("session-manager", err)

    def start_stall_monitor(self) -> None:
        """Background monitor: report sessions that are turn-active but
        produced no output for stall_timeout_s (only notify, never kill)."""
        if getattr(self, "_stall_task", None):
            self._stall_task.cancel()
        self._stall_task = asyncio.get_event_loop().create_task(
            self._stall_monitor())

    def stop_stall_monitor(self) -> None:
        """Audit M1: cancel the monitor on shutdown so it never touches a
        closed db / torn-down host."""
        task = getattr(self, "_stall_task", None)
        if task is not None:
            task.cancel()
            self._stall_task = None

    async def _stall_monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._stall_check_once()
        except asyncio.CancelledError:
            pass

    async def _stall_check_once(self) -> None:
        """One stall sweep: report turn-active sessions with no output for
        stall_timeout_s. Each session is reported at most once per minute."""
        stall_timeout = float(self._restart_config.get("stall_timeout_s", 900))
        now = time.time()
        for sid, s in self.sessions.items():
            if not s.get("turn_active"):
                continue
            last_out = s.get("last_output_at") or 0
            if now * 1000 - last_out > stall_timeout * 1000:
                if self._stall_reported.get(sid) != now // 60:
                    self._stall_reported[sid] = now // 60
                    self._emit("session_event", {
                        "type": "stalled", "session_id": sid,
                        "name": s.get("name"),
                        "tool": s.get("tool"),
                        "project": s.get("cwd"),
                        "state": s.get("state"),
                        "exit_code": None, "signal": None,
                        "ts": now,
                    })

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

    def _schedule_auto_resume(self, session: dict) -> None:
        """Kill the hung reasonix session and restart it with `-c --copy`.

        Called when the output stream contains 'session is in use'. The
        current process printed the error and hangs; kill it, then spawn a
        duplicated conversation.
        """
        sid = session["id"]

        async def _do() -> None:
            try:
                await self.stop(sid)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
            try:
                await self._start_pty_retry_copy(self.sessions.get(sid))
            except Exception:  # noqa: BLE001
                pass

        asyncio.get_event_loop().create_task(_do())

    # --- helpers -------------------------------------------------------------------------
    def _inflate(self, stored: dict) -> dict:
        tool = self.config.get("tools", {}).get(stored.get("tool")) or {}
        engine = tool.get("engine", "pty")
        import asyncio as _aio
        return {
            "id": stored.get("id") or str(uuid.uuid4()),
            "name": stored.get("name"),
            "cwd": stored.get("cwd"),
            "tool": stored.get("tool"),
            "args": stored.get("args") or "",
            "permissionMode": stored.get("permissionMode"),
            "last_error": stored.get("last_error"),
            "autostart": bool(stored.get("autostart")),
            # Serializes start/stop/remove per session (Issue 3: no races
            # where stop kills a freshly restarted process).
            "_lock": _aio.Lock(),
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
            "transcript": self._load_transcript(stored) if engine == "agent" else [],
            "turn_active": False,
            "busy": False,
            "last_output_at": None,
            "recent_buf": RingBuffer(RECENT_BUF_CAP) if engine == "pty" else None,
        }

    def _load_transcript(self, stored: dict) -> list:
        """Audit T1: recover the chat history from the incremental JSONL
        (last AGENT_MAX_ITEMS lines). Missing/corrupt → empty."""
        sid = stored.get("id")
        log_path = stored.get("logPath") or stored.get("log_path") or ""
        base_dir = os.path.dirname(log_path) or logs_dir
        tpath = os.path.join(base_dir, f"{sid}.transcript.jsonl")
        out: list[dict] = []
        try:
            if not os.path.isfile(tpath):
                return out
            with open(tpath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if len(out) > AGENT_MAX_ITEMS:
                        del out[:len(out) - AGENT_MAX_ITEMS]
        except OSError:
            return []
        return out

    def _persist(self) -> None:
        self.config["sessions"] = [
            {
                "id": s["id"], "name": s.get("name"), "cwd": s.get("cwd"),
                "tool": s.get("tool"), "args": s.get("args"), "autostart": s.get("autostart"),
                "logPath": s.get("log_path"),
                "agentSessionId": s.get("agent_session_id"),
                "permissionMode": s.get("permissionMode"),
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
            "args": (session.get("args") or "")[:200],
            "permissionMode": session.get("permissionMode"),
            "last_error": session.get("last_error"),
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
        # reasonix-family: "session is in use" is printed to the terminal and
        # then the process HANGS (it does not exit) — the session would sit
        # frozen forever. Detect the error in the output stream and restart
        # with `-c --copy` (verified: that combination always works, even in
        # a fresh project dir).
        if (session.get("tool") in ("reasonix", "opencode")
                and session.get("state") == "running"
                and not session.get("_resume_retried")):
            # "in use" may arrive split across chunks — scan a window of
            # recent output.
            window = b""
            buf = session.get("recent_buf")
            if buf is not None:
                try:
                    window = buf.snapshot()[-512:]
                except Exception:  # noqa: BLE001
                    window = b""
            window += chunk[-512:]
            if b"in use" in window.lower():
                session["_resume_retried"] = True
                self._schedule_auto_resume(session)

    # Cached per-session log file handle: opening/closing the log on every
    # output chunk (~60/s on an active terminal) was pure syscall overhead.
    def _append_log_cached(self, session: dict, chunk: bytes) -> None:
        try:
            fh = session.get("_log_fh")
            if fh is None:
                fh = open(session["log_path"], "ab", buffering=8192)
                session["_log_fh"] = fh
                session["_log_bytes"] = 0
            fh.write(chunk)
            # Running-session rotation (audit L1): buffered writes bypass
            # _append_log's size check, so a long-lived reasonix session's
            # log grew unboundedly (~60 writes/s). Count bytes and check the
            # size every ~512KB; at 5MB rotate to .1 and reopen the handle.
            session["_log_bytes"] = session.get("_log_bytes", 0) + len(chunk)
            if session["_log_bytes"] >= 512 * 1024:
                session["_log_bytes"] = 0
                try:
                    if os.path.getsize(session["log_path"]) > 5 * 1024 * 1024:
                        fh.flush()
                        fh.close()
                        os.replace(session["log_path"], session["log_path"] + ".1")
                        session["_log_fh"] = open(session["log_path"], "ab", buffering=8192)
                except OSError:
                    pass
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
        # Audit K: reasonix/opencode sessions stay "engaged" (busy dot
        # keeps blinking) while their process is alive — a long silent
        # think would otherwise look idle with the 5s window.
        if session.get("tool") in ("reasonix", "opencode"):
            if not session.get("_engaged"):
                session["_engaged"] = True
                self._emit("change", self._public(session))
            return  # no idle deadline while the agent is running
        # call_later is far lighter than create_task (no coroutine/task
        # object churn at ~60 frames/s on an active terminal). Cancel +
        # reschedule the deadline instead of stacking tasks.
        handle = session.get("_busy_handle")
        if handle is not None:
            handle.cancel()

        loop = asyncio.get_event_loop()
        session["_busy_handle"] = loop.call_later(
            BUSY_IDLE_MS / 1000,
            lambda: self._mark_idle(session))

    def _mark_idle(self, session: dict) -> None:
        session["_busy_handle"] = None
        if session.get("tool") in ("reasonix", "opencode"):
            session["_engaged"] = False  # process exited or stalled
        if session.get("busy"):
            session["busy"] = False
            self._emit("change", self._public(session))
