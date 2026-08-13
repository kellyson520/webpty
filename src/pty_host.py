"""PTY host — detached daemon that owns PTY processes on behalf of webpty.

Survives webpty restarts so claude / codex / pwsh sessions don't die when the
HTTP server is bounced. Listens on a Unix socket (Windows named-pipe support
is not available in the stdlib; on win32 webpty falls back to in-process
PTYs). Line-delimited JSON protocol; PTY output is base64 in the payload.

Runs with Python's standard library only (pty, os.fork, selectors).
"""
from __future__ import annotations

import base64
import json
import os
import selectors
import signal
import socket
import struct
import sys
import time
from collections.abc import Iterable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from ring_buffer import RingBuffer  # noqa: E402

# --- platform dispatch ------------------------------------------------------
# Windows has no pty.fork in the stdlib; a pywinpty backend
# (pty_host_windows) takes over there. Importing pty_host must never start
# a socket — only the __main__ guard below runs a server — and the
# POSIX-only `pty` module is imported lazily so a plain `import pty_host`
# works on Windows too.
if os.name == "nt":
    _backend = "winpty"
    from pty_host_windows import main as _host_main  # noqa: E402
else:
    _backend = "forkpty"
    import pty  # noqa: E402

PIPE_NAME = (
    os.environ.get("WEBPTY_PTY_HOST_PIPE")
    or ("/tmp/webpty-pty-host.sock" if os.name == "posix" else "webpty-pty-host")
)
BUFFER_CAP = 256 * 1024  # per-session scrollback for replay on reattach
HOST_VERSION = 1
MAX_OUTPUT_BYTES = 32768  # max bytes per merged output frame
FLUSH_DELAY = 0.016  # seconds to wait before flushing pending output

# --- output merging --------------------------------------------------------


def merge_chunks(
    chunks: Iterable[bytes], max_bytes: int = MAX_OUTPUT_BYTES
) -> list[bytes]:
    """Merge small byte chunks into fewer larger ones (≤ max_bytes each).

    The order and total content of the input is preserved; only the frame
    boundaries change, so the pty-host protocol (one base64 JSON line per
    frame) stays identical from the client's point of view.
    """
    merged: list[bytes] = []
    current = bytearray()
    for c in chunks:
        if not c:
            continue
        if current and len(current) + len(c) > max_bytes:
            merged.append(bytes(current))
            current = bytearray()
        if len(c) >= max_bytes:
            # A single chunk that already exceeds the cap is split by itself.
            if current:
                merged.append(bytes(current))
                current = bytearray()
            for i in range(0, len(c), max_bytes):
                merged.append(c[i:i + max_bytes])
        else:
            current += c
    if current:
        merged.append(bytes(current))
    return merged

# --- sessions ---------------------------------------------------------------
# sid -> {"pid", "master_fd", "buffer", "clients": set, "cols", "rows",
#         "alive", "exit_code", "exit_signal", "started_at"}
sessions: dict[str, dict] = {}
sel = selectors.DefaultSelector()


def _send(sock: socket.socket, msg: dict) -> None:
    try:
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    except OSError:
        pass


def _broadcast(session: dict, line: str) -> None:
    data = line.encode("utf-8")
    for c in list(session["clients"]):
        try:
            c.sendall(data)
        except OSError:
            # Sender's socket buffer is full and the server isn't draining
            # fast enough. Silently dropping the client (previous behavior)
            # permanently severed output for that connection with no signal
            # to the server — it only noticed after a page refresh. Instead:
            # remove the client AND tell it to resync so the next chunk is
            # preceded by a full state snapshot (never a silent gap).
            session["clients"].discard(c)
            try:
                c.sendall((json.dumps({
                    "ev": "dropped", "id": session["id"],
                    "pid": os.getpid(),
                }) + "\n").encode("utf-8"))
            except OSError:
                pass


def _flush_output(session: dict) -> None:
    """Merge pending chunks and broadcast one frame per merged piece."""
    if session["pending"]:
        for merged in merge_chunks(session["pending"], max_bytes=MAX_OUTPUT_BYTES):
            line = json.dumps({
                "ev": "output", "id": session["id"],
                "data": base64.b64encode(merged).decode("ascii"),
            }) + "\n"
            _broadcast(session, line)
        session["pending"] = []
    session["last_flush"] = time.monotonic()


def _flush_expired(now: float) -> None:
    """Flush any session whose pending output has exceeded the flush delay."""
    for session in sessions.values():
        if session["pending"] and now - session["last_flush"] >= FLUSH_DELAY:
            _flush_output(session)


def _drain_output(session: dict) -> None:
    """Non-blockingly read any remaining PTY output into buffer/pending.

    Called before a session is dropped: a child that prints its final output
    and exits may be reaped before the read branch of the event loop runs,
    leaving that tail unread in the master fd. Draining here prevents it from
    being lost when the fd is closed.
    """
    while True:
        try:
            chunk = os.read(session["master_fd"], 65536)
        except OSError:
            break  # EAGAIN (nothing more) or EIO (child gone)
        if not chunk:
            break
        session["buffer"].push(chunk)
        session["pending"].append(chunk)


def handle_start(sock: socket.socket, msg: dict) -> None:
    import fcntl
    import struct
    sid = msg.get("id")
    if sid in sessions:
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": sid,
                     "message": "already started"})
        return

    if os.name != "posix":
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": sid,
                     "message": "pty-host requires POSIX (Python stdlib has no Windows PTY)"})
        return

    cmd = msg.get("command") or ""
    args = [str(a) for a in (msg.get("args") or [])]
    cwd = msg.get("cwd") or os.getcwd()
    # Audit H1: defensive int parsing — bad types previously raised
    # ValueError inside on_line (crashed the whole host).
    try:
        cols = max(1, min(int(msg.get("cols") or 120), 1000))
        rows = max(1, min(int(msg.get("rows") or 30), 1000))
    except (TypeError, ValueError):
        cols, rows = 120, 30

    env = dict(os.environ)
    for k, v in (msg.get("env") or {}).items():
        env[str(k)] = str(v)
    env.setdefault("TERM", "xterm-256color")

    try:
        # pty.fork() wires the slave to 0/1/2 and makes it the controlling
        # terminal (setsid + TIOCSCTTY) — required for TIOCSWINSZ to take
        # effect. We set the window size INSIDE the child right before exec:
        # setting it in the parent after fork() is racy (the child may exec and
        # read ws_row=0/ws_col=0, which makes full-screen TUIs like reasonix
        # render a 0x0 layout and go black).
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, 0x5414, struct.pack("HH", rows, cols))  # TIOCSWINSZ on the controlling tty
                os.chdir(cwd)
                os.environ.clear()
                os.environ.update(env)
                # Close every inherited fd above stdio before exec. pty-host
                # holds the listening unix socket, client sockets and the
                # selectors epoll fd; leaking them lets TUIs mistake a stray
                # readable fd for an event source and busy-loop.
                try:
                    os.closerange(3, 1024)
                except OSError:
                    pass
                os.execvp(cmd, [cmd] + args)
            except Exception as err:  # noqa: BLE001 — child must not raise
                os.write(2, f"[pty-host] exec failed: {err} (command={cmd!r}, cwd={cwd!r})\n".encode())
                os._exit(127)
    except OSError as err:
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": sid,
                     "message": str(err)})
        return

    # Parent: make sure the winsize is set on the master too (mirrors the
    # child's setting; ioctl on either end of a pty updates both).
    try:
        fcntl.ioctl(master_fd, 0x5414, struct.pack("HH", rows, cols))  # TIOCSWINSZ
    except OSError:
        pass

    os.set_blocking(master_fd, False)
    session = {
        "id": sid,
        "pid": pid,
        "master_fd": master_fd,
        "buffer": RingBuffer(BUFFER_CAP),
        "clients": set(),
        "pending": [],
        "last_flush": time.monotonic(),
        "cols": cols,
        "rows": rows,
        "alive": True,
        "exit_code": None,
        "exit_signal": None,
        "command": cmd,
        "args": args,
        "cwd": cwd,
        "started_at": int(time.time() * 1000),
    }
    sessions[sid] = session
    sel.register(master_fd, selectors.EVENT_READ, ("output", sid))

    try:
        import fcntl
        import struct

        fcntl.ioctl(master_fd, 0x5414, struct.pack("HH", rows, cols))  # TIOCSWINSZ
    except Exception:  # noqa: BLE001 — window size is best-effort
        pass

    _send(sock, {"ev": "started", "reqId": msg.get("reqId"), "id": sid, "pid": pid})


def handle_attach(sock: socket.socket, msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if not session:
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": msg.get("id"),
                     "message": "not found"})
        return
    # Flush pending output to the existing clients first, so the new client's
    # replay (which already includes that data via the buffer) is not duplicated
    # by a later flush frame.
    if session["pending"]:
        _flush_output(session)
    session["clients"].add(sock)
    st = _client_state.get(sock.fileno())
    if st:
        st["sessions"].add(msg.get("id"))
    replay = session["buffer"].snapshot()
    _send(sock, {
        "ev": "attached",
        "reqId": msg.get("reqId"),
        "id": msg.get("id"),
        "cols": session["cols"],
        "rows": session["rows"],
        "pid": session["pid"],
        "alive": session["alive"],
        "exit_code": session["exit_code"],
        "exit_signal": session["exit_signal"],
        "replay": base64.b64encode(replay).decode("ascii"),
    })


def handle_detach(sock: socket.socket, msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if not session:
        return
    session["clients"].discard(sock)
    st = _client_state.get(sock.fileno())
    if st:
        st["sessions"].discard(msg.get("id"))


def handle_input(msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if not session or not session["alive"]:
        return
    data = base64.b64decode(msg.get("data") or "")
    try:
        os.write(session["master_fd"], data)
    except OSError:
        pass


def handle_resize(msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if not session:
        return
    # Audit H1: bad types here previously raised ValueError inside on_line.
    try:
        cols = int(msg["cols"]) if msg.get("cols") is not None else session["cols"]
        rows = int(msg["rows"]) if msg.get("rows") is not None else session["rows"]
    except (TypeError, ValueError):
        return
    session["cols"] = max(1, min(cols, 1000))
    session["rows"] = max(1, min(rows, 1000))
    if session["alive"]:
        try:
            import fcntl
            import struct

            fcntl.ioctl(session["master_fd"], 0x5414,
                        struct.pack("HH", session["rows"], session["cols"]))
        except Exception:  # noqa: BLE001
            pass


def _kill_session(session: dict) -> None:
    try:
        os.kill(session["pid"], signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def handle_kill(sock: socket.socket, msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if session:
        _kill_session(session)
    _send(sock, {"ev": "killed", "reqId": msg.get("reqId"), "id": msg.get("id")})


def handle_forget(sock: socket.socket, msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if session and session["alive"]:
        _kill_session(session)
    _drop_session(msg.get("id"))
    _send(sock, {"ev": "forgotten", "reqId": msg.get("reqId"), "id": msg.get("id")})


def handle_list(sock: socket.socket, msg: dict) -> None:
    out = []
    for s in sessions.values():
        out.append({
            "id": s["id"], "pid": s["pid"], "cols": s["cols"], "rows": s["rows"],
            "alive": s["alive"], "exit_code": s["exit_code"], "exit_signal": s["exit_signal"],
            "command": s["command"], "args": s["args"], "cwd": s["cwd"],
            "started_at": s["started_at"],
        })
    _send(sock, {"ev": "list", "reqId": msg.get("reqId"), "sessions": out})


def on_line(sock: socket.socket, line: str) -> None:
    # Audit H1: ANY malformed message must be ignored, never crash the
    # daemon (a crash kills every session). JSON-valid-but-wrong-shaped
    # payloads (list, null, missing fields, bad types) previously raised
    # AttributeError/TypeError/ValueError inside the handlers.
    try:
        msg = json.loads(line)
        if not isinstance(msg, dict):
            return
        op = msg.get("op")
        if op == "list":
            handle_list(sock, msg)
        elif op == "start":
            handle_start(sock, msg)
        elif op == "attach":
            handle_attach(sock, msg)
        elif op == "detach":
            handle_detach(sock, msg)
        elif op == "input":
            handle_input(msg)
        elif op == "resize":
            handle_resize(msg)
        elif op == "kill":
            handle_kill(sock, msg)
        elif op == "forget":
            handle_forget(sock, msg)
    except Exception:  # noqa: BLE001 — malformed input must not kill the host
        pass


def _drop_session(sid: str) -> None:
    session = sessions.pop(sid, None)
    if not session:
        return
    _drain_output(session)
    if session["pending"]:
        _flush_output(session)
    try:
        sel.unregister(session["master_fd"])
    except (KeyError, ValueError):
        pass
    try:
        os.close(session["master_fd"])
    except OSError:
        pass
    for c in list(session["clients"]):
        session["clients"].discard(c)


def _reap_children() -> None:
    """Collect exited PTY children and broadcast their exit events."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        for sid, session in list(sessions.items()):
            if session["pid"] == pid:
                session["alive"] = False
                if os.WIFEXITED(status):
                    session["exit_code"] = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    session["exit_signal"] = os.WTERMSIG(status)
                _drain_output(session)
                if session["pending"]:
                    _flush_output(session)
                line = json.dumps({
                    "ev": "exit", "id": sid,
                    "code": session["exit_code"], "signal": session["exit_signal"],
                }) + "\n"
                _broadcast(session, line)
                _drop_session(sid)
                break


# Per-client-socket read state: sock.fileno() -> {"buf": bytearray, "sessions": set}
_client_state: dict[int, dict] = {}


def _client_cleanup(sock: socket.socket) -> None:
    try:
        sel.unregister(sock)
    except (KeyError, ValueError):
        pass
    st = _client_state.pop(sock.fileno(), None)
    if st:
        for sid in st["sessions"]:
            session = sessions.get(sid)
            if session:
                session["clients"].discard(sock)
    try:
        sock.close()
    except OSError:
        pass


def _client_read(sock: socket.socket) -> None:
    st = _client_state.get(sock.fileno())
    if st is None:
        _client_cleanup(sock)
        return
    try:
        data = sock.recv(65536)
    except OSError:
        _client_cleanup(sock)
        return
    if not data:
        _client_cleanup(sock)
        return
    st["buf"].extend(data)
    # Audit L3/L5: a misbehaving peer can stream data faster than we parse;
    # cap the per-connection input buffer at 1MB. Dropping the connection
    # used to kill ALL sessions' input path (every session shares this
    # socket) — instead drop the offending bytes and keep the connection
    # (the client's own chunking bounds real-world input; this only guards
    # against a runaway/malicious peer).
    if len(st["buf"]) > 1024 * 1024:
        st["buf"] = st["buf"][-1024 * 1024:]
        return
    while b"\n" in st["buf"]:
        line, _, rest = st["buf"].partition(b"\n")
        st["buf"][:] = rest
        if line.strip():
            on_line(sock, line.decode("utf-8", "replace"))


def on_connection(server: socket.socket) -> None:
    sock, _addr = server.accept()
    # Security (Issue 2.4): only the user who started pty-host may connect —
    # otherwise any local user could spawn arbitrary commands (privilege
    # escalation if webpty runs as root). Verify the peer's UID via
    # SO_PEERCRED and drop others.
    try:
        cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        peer_uid = struct.unpack("3i", cred)[1]  # pid, uid, gid
    except (OSError, struct.error):
        try:
            sock.close()
        except OSError:
            pass
        return
    if peer_uid != os.getuid():
        try:
            sock.close()
        except OSError:
            pass
        return
    sock.setblocking(False)
    try:
        # 1MB send buffer: bursts (multi-session TUI repaints) fit without
        # hitting BlockingIOError mid-frame; paired with the "dropped"
        # resync signal in _broadcast this makes the pipe lossless.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
    except OSError:
        pass
    state = {"buf": bytearray(), "sessions": set()}
    _client_state[sock.fileno()] = state
    _send(sock, {"ev": "hello", "version": HOST_VERSION, "pid": os.getpid()})
    sel.register(sock, selectors.EVENT_READ, ("client", sock))


def main() -> None:
    # Unix socket path needs cleanup if a previous run left it behind.
    try:
        os.unlink(PIPE_NAME)
    except OSError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(PIPE_NAME)
    try:
        os.chmod(PIPE_NAME, 0o700)  # only the owner may connect (Issue 2.4)
    except OSError:
        pass
    server.listen(16)
    server.setblocking(False)
    sel.register(server, selectors.EVENT_READ, ("accept", server))
    print(f"[pty-host] listening on {PIPE_NAME}, pid={os.getpid()}", flush=True)

    def shutdown(signum, _frame):  # type: ignore[no-untyped-def]
        print(f"[pty-host] shutting down ({signal.Signals(signum).name})", flush=True)
        for session in list(sessions.values()):
            _kill_session(session)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        signal.signal(signal.SIGHUP, shutdown)
    except (AttributeError, ValueError):
        pass  # SIGHUP may be unavailable on some platforms

    try:
        while True:
            _reap_children()
            # Adaptive select timeout: 16ms when there is pending output to
            # flush (keeps latency low), 1s when idle — a host with no
            # sessions was waking 62×/s doing nothing (low-footprint core).
            _now = time.monotonic()
            # BUGFIX (latency test): the old condition ALSO required
            # now-last_flush >= FLUSH_DELAY — right after a flush, fresh
            # pending output (the tail of a command's output arriving
            # within 16ms) failed the check, the select timeout fell back
            # to 1.0s, and the output was delayed a FULL second (the
            # interactive terminal felt laggy). Any pending bytes must
            # keep the timeout short; _flush_expired handles the due
            # check itself.
            has_pending = any(s.get("pending") for s in sessions.values())
            timeout = FLUSH_DELAY if has_pending else 1.0
            events = sel.select(timeout=timeout)
            _flush_expired(time.monotonic())
            for key, _mask in events:
                # Audit H1: one bad event must never kill the daemon and
                # all its sessions — log and continue.
                try:
                    kind = key.data
                    if kind[0] == "accept":
                        on_connection(kind[1])
                    elif kind[0] == "client":
                        sock = kind[1]
                        _client_read(sock)
                    elif kind[0] == "output":
                        sid = kind[1]
                        session = sessions.get(sid)
                        if not session:
                            continue
                        try:
                            chunk = os.read(session["master_fd"], 65536)
                        except OSError:
                            continue
                        if chunk:
                            session["buffer"].push(chunk)
                            session["pending"].append(chunk)
                            pending_bytes = sum(len(c) for c in session["pending"])
                            if (pending_bytes >= MAX_OUTPUT_BYTES
                                    or time.monotonic() - session["last_flush"] >= FLUSH_DELAY):
                                _flush_output(session)
                except Exception:  # noqa: BLE001
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        for session in list(sessions.values()):
            _drain_output(session)
            _flush_output(session)
            _kill_session(session)
        sel.close()
        server.close()
        try:
            os.unlink(PIPE_NAME)
        except OSError:
            pass


if __name__ == "__main__":
    if os.name == "nt":
        from pty_host_windows import run_windows_host
        run_windows_host()
    else:
        main()
