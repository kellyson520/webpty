"""Windows PTY host — pywinpty backend. Same JSON-line protocol as pty_host.

On POSIX the host runs as a Unix-socket daemon around pty.fork
(``pty_host.py``). Windows has no stdlib PTY support, so this module
implements the same line-delimited JSON protocol on top of pywinpty's
``PtyProcess`` (a wrapper around the WinPTY console emulator). The webpty
client (``pty_host_client.py``) talks to either backend identically.

This file is never executed on POSIX. pywinpty is imported lazily inside
``run_windows_host`` so a missing dependency fails fast with a clear
message instead of raising ImportError at module load time.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import selectors
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ring_buffer import RingBuffer  # noqa: E402

# Named-pipe/tcp name mirrors pty_host.PIPE_NAME (webpty passes the same env
# override to both backends).
PIPE_NAME = os.environ.get("WEBPTY_PTY_HOST_PIPE") or "webpty-pty-host"
BUFFER_CAP = 256 * 1024  # per-session scrollback for replay on reattach
HOST_VERSION = 1
MAX_OUTPUT_BYTES = 32768  # max bytes per merged output frame
FLUSH_DELAY = 0.016  # seconds to wait before flushing pending output

# sid -> session; same layout as pty_host.sessions.
sessions: dict[str, dict] = {}
sel = selectors.DefaultSelector()


def run_windows_host() -> None:
    """Serve the pty-host protocol with a winpty.PtyProcess backend.

    Deliberately a skeleton on POSIX: this module is never executed here, and
    full Windows behavior is validated in a Windows/CI environment. The
    protocol and dispatch layer are what this task ships.
    """
    try:
        import winpty  # noqa: F401
    except ImportError:
        print(
            "[pty-host] pywinpty not installed — run: pip install -r "
            "requirements-windows.txt",
            flush=True,
        )
        sys.exit(1)

    # ---- design notes for the Windows implementation (see task-7 report) ----
    #
    # Socket service (same protocol as pty_host.main()):
    #   * Listen on a Windows named pipe (or localhost TCP if named-pipe
    #     support proves awkward) and send {"ev": "hello", "version":
    #     HOST_VERSION, "pid": os.getpid()} on accept.
    #
    # handle_start — winpty has no fd-based master; PtyProcess is an object:
    #     proc = winpty.PtyProcess.start(cmd, args, cwd, cols, rows, env)
    #   * command/args/cwd/cols/rows/env come from the same start message
    #     fields pty_host reads; default TERM=xterm-256color in env.
    #   * input -> proc.write(data)            (pty_host: os.write(master_fd))
    #   * resize -> proc.setwinsize(rows, cols) (pty_host: fcntl TIOCSWINSZ)
    #   * kill -> proc.kill() / proc.terminate()
    #   * session dict keeps pty_host's keys: id/pid/buffer/clients/pending/
    #     last_flush/cols/rows/alive/exit_code/exit_signal/command/args/cwd/
    #     started_at. Reuse pty_host's RingBuffer for the replay buffer.
    #
    # Output forwarding — no selectable fd on Windows, so:
    #   * one background thread per session loops proc.read() (blocking) and
    #     pushes each chunk into session["buffer"] + session["pending"], then
    #     into a shared queue.Queue consumed by the event loop.
    #   * the selectors loop runs on timeout=FLUSH_DELAY, drains the queue and
    #     flushes merged output exactly like pty_host: merge to
    #     <= MAX_OUTPUT_BYTES frames, base64 them and broadcast
    #     {"ev": "output", "id", "data"} to attached clients.
    #   * when the reader thread sees proc exit it puts a sentinel; the loop
    #     then flushes remaining pending output, broadcasts
    #     {"ev": "exit", "id", "code", "signal"} and drops the session.
    #
    # ops handled (identical to pty_host.on_line): list / start / attach /
    # detach / input / resize / kill / forget. attach replies with
    # {"ev": "attached", ..., "replay": base64(buffer.snapshot())}.
    #
    # NOTE: do not `from pty_host import ...` at module top level — pty_host
    # imports this module during its own platform dispatch, so a top-level
    # cycle would fail before pty_host finishes initialising. If protocol
    # helpers (e.g. merge_chunks) are needed, import them lazily inside
    # run_windows_host, by which point pty_host is fully initialised.
    raise NotImplementedError(
        "[pty-host] winpty backend is a skeleton in this build; the full "
        "Windows implementation is deferred to a Windows/CI run."
    )


# Alias for pty_host's platform dispatch (`from pty_host_windows import main`).
main = run_windows_host


if __name__ == "__main__":
    run_windows_host()
