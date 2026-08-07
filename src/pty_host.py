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
import pty
import selectors
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from ring_buffer import RingBuffer  # noqa: E402

PIPE_NAME = (
    os.environ.get("WEBPTY_PTY_HOST_PIPE")
    or ("/tmp/webpty-pty-host.sock" if os.name == "posix" else "webpty-pty-host")
)
BUFFER_CAP = 256 * 1024  # per-session scrollback for replay on reattach
HOST_VERSION = 1

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
            session["clients"].discard(c)


def handle_start(sock: socket.socket, msg: dict) -> None:
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
    cols = int(msg.get("cols") or 120)
    rows = int(msg.get("rows") or 30)

    env = dict(os.environ)
    for k, v in (msg.get("env") or {}).items():
        env[str(k)] = str(v)
    env.setdefault("TERM", "xterm-256color")

    try:
        pid, master_fd = pty.fork()
    except OSError as err:
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": sid,
                     "message": str(err)})
        return

    if pid == 0:
        # Child: exec the command.
        try:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(env)
            os.execvp(cmd, [cmd] + args)
        except Exception as err:  # noqa: BLE001 — child must not raise
            os.write(2, f"[pty-host] exec failed: {err}\n".encode())
            os._exit(127)

    # Parent.
    os.set_blocking(master_fd, False)
    session = {
        "id": sid,
        "pid": pid,
        "master_fd": master_fd,
        "buffer": RingBuffer(BUFFER_CAP),
        "clients": set(),
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

        fcntl.ioctl(master_fd, 0x5413, struct.pack("HH", rows, cols))  # TIOCSWINSZ
    except Exception:  # noqa: BLE001 — window size is best-effort
        pass

    _send(sock, {"ev": "started", "reqId": msg.get("reqId"), "id": sid, "pid": pid})


def handle_attach(sock: socket.socket, msg: dict) -> None:
    session = sessions.get(msg.get("id"))
    if not session:
        _send(sock, {"ev": "error", "reqId": msg.get("reqId"), "id": msg.get("id"),
                     "message": "not found"})
        return
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
    session["cols"] = int(msg.get("cols") or session["cols"])
    session["rows"] = int(msg.get("rows") or session["rows"])
    if session["alive"]:
        try:
            import fcntl
            import struct

            fcntl.ioctl(session["master_fd"], 0x5413,
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
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
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


def _drop_session(sid: str) -> None:
    session = sessions.pop(sid, None)
    if not session:
        return
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
    while b"\n" in st["buf"]:
        line, _, rest = st["buf"].partition(b"\n")
        st["buf"][:] = rest
        if line.strip():
            on_line(sock, line.decode("utf-8", "replace"))


def on_connection(server: socket.socket) -> None:
    sock, _addr = server.accept()
    sock.setblocking(False)
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
            events = sel.select(timeout=0.5)
            for key, _mask in events:
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
                        line = json.dumps({
                            "ev": "output", "id": sid,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }) + "\n"
                        _broadcast(session, line)
    except KeyboardInterrupt:
        pass
    finally:
        for session in list(sessions.values()):
            _kill_session(session)
        sel.close()
        server.close()
        try:
            os.unlink(PIPE_NAME)
        except OSError:
            pass


if __name__ == "__main__":
    main()
