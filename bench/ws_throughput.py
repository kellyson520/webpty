#!/usr/bin/env python3
"""bench/ws_throughput.py — WebSocket echo latency (p95) + server memory peak.

记录用基准脚本（非 CI gate）。纯标准库、Python ≥ 3.10，独立运行：

    python3 bench/ws_throughput.py [port]

行为：
  * 若 <port>（默认 4790）上已有 webpty 在运行 → 直接复用它；
  * 否则在目标端口临时启动一个隔离实例（临时 data dir + projects
    root，独立进程），测完即关；
  * 创建 bash 会话 → WebSocket 连接 → 逐轮 `echo __BENCH_<n>__`
    回显测往返延迟（含预热轮）；
  * 可选突发吞吐（--burst N：连发 N 条 echo，报告 msgs/s）；
  * 测量期间轮询服务器进程 VmRSS（Linux /proc/<pid>/status）取峰值，
    非 Linux 或定位不到进程时记为 n/a；
  * 输出 p50/mean/p95/p99/max/min 延迟 + 内存峰值。

选项：
  --rounds N        正式测量轮数（默认 50）
  --warmup N        预热轮数，不计入统计（默认 5）
  --burst N         突发吞吐命令条数（默认 200，设 0 关闭）
  --json            输出单行 JSON 取代人类可读报告
  --no-self-host    端口无服务时直接失败，不自动起临时服务器
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SERVER = os.path.join(_ROOT, "src", "server.py")
DEFAULT_PORT = 4790
_TOKEN_PREFIX = "__BENCH__"


# ---------------------------------------------------------------------------
# minimal HTTP + WebSocket client (stdlib only)
# ---------------------------------------------------------------------------
async def _http(method: str, port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else b""
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Connection: keep-alive\r\nContent-Type: application/json\r\n"
    )
    if data:
        req += f"Content-Length: {len(data)}\r\n"
    req += "\r\n"
    writer.write(req.encode("latin-1") + data)
    await writer.drain()
    status_line = await reader.readline()
    status = int(status_line.split()[1])
    length = None
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n"):
            break
        k, _, v = line.decode("latin-1").partition(":")
        if k.strip().lower() == "content-length":
            length = int(v.strip())
    raw = b""
    if length is not None:
        while len(raw) < length:
            chunk = await reader.read(length - len(raw))
            if not chunk:
                break
            raw += chunk
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return status, {}


class _WSClient:
    """Minimal RFC 6455 client: masked text frames + ping/pong + close."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._buf = bytearray()

    async def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.writer.write(header + mask + masked)
        await self.writer.drain()

    async def _read_more(self, timeout: float) -> bool:
        if len(self._buf) >= 2:
            return True
        try:
            chunk = await asyncio.wait_for(self.reader.read(65536), timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    async def recv_payload(self, timeout: float = 5.0) -> bytes | None:
        """Return the next text/binary payload, or None on close/timeout."""
        while True:
            if not await self._read_more(timeout):
                return None
            b0, b1 = self._buf[0], self._buf[1]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            off = 2
            if length == 126:
                if len(self._buf) < 4 and not await self._read_more(timeout):
                    return None
                length = struct.unpack(">H", self._buf[2:4])[0]
                off = 4
            elif length == 127:
                if len(self._buf) < 10 and not await self._read_more(timeout):
                    return None
                length = struct.unpack(">Q", self._buf[2:10])[0]
                off = 10
            if len(self._buf) < off + length:
                try:
                    chunk = await asyncio.wait_for(
                        self.reader.read(max(off + length - len(self._buf), 4096)),
                        timeout=timeout)
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    return None
                if not chunk:
                    return None
                self._buf += chunk
            payload = bytes(self._buf[off:off + length])
            del self._buf[:off + length]
            if opcode == 0x9:  # ping -> pong
                mask = os.urandom(4)
                n = len(payload)
                header = bytes([0x8A, 0x80 | n]) if n < 126 else bytes([0x8A, 0x80 | 126]) + struct.pack(">H", n)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                self.writer.write(header + mask + masked)
                await self.writer.drain()
                continue
            if opcode in (0x1, 0x2):
                return payload
            if opcode == 0x8:
                return None

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _ws_connect(port: int, path: str) -> _WSClient:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode())
    await writer.drain()
    head = await reader.readline()
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n"):
            break
    if b"101" not in head:
        writer.close()
        raise RuntimeError(f"WS handshake failed: {head!r}")
    return _WSClient(reader, writer)


# ---------------------------------------------------------------------------
# server process discovery / RSS sampling (Linux /proc)
# ---------------------------------------------------------------------------
def _pid_listening_on(port: int) -> int | None:
    want = "%04X" % port
    for procfile in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(procfile) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    local, state = parts[1], parts[3]
                    if local.rsplit(":", 1)[-1] == want and state == "0A":
                        pid = _pid_for_inode(parts[9])
                        if pid is not None:
                            return pid
        except OSError:
            continue
    return None


def _pid_for_inode(inode: str) -> int | None:
    needle = f"socket:[{inode}]"
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            for link in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, link)).endswith(needle):
                        return int(entry)
                except OSError:
                    pass
        except OSError:
            pass
    return None


def _rss_kb(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _children_pids(pid: int) -> list[int]:
    """Direct children of a process via /proc/<pid>/task/<pid>/children.

    Linux-specific; the pty-host daemon spawned by a webpty server is its
    direct child (Popen with start_new_session changes the session, not the
    parent), so this is a precise handle on "the pty-host this server
    spawned" — unlike scanning all of /proc for pty_host.py cmdlines, which
    also catches pty-hosts spawned by other webpty servers on the host.
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except OSError:
        return []


def _is_zombie(pid: int) -> bool:
    """True once the process has terminated but not yet been reaped.

    A zombie is already dead — SIGTERM worked — so the caller can stop
    waiting on it even though /proc/<pid> still exists and os.kill(pid, 0)
    still succeeds.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
            rparen = data.rfind(b")")
            return data[rparen + 2:rparen + 3] == b"Z"
    except OSError:
        return False


def _terminate_pids(pids: list[int], timeout_s: float = 3.0) -> None:
    """SIGTERM a set of pids and wait for them to exit (SIGKILL as fallback).

    pty_host.py installs a SIGTERM handler that kills its PTY sessions and
    unlinks its socket, so a clean TERM is enough for a graceful teardown.
    """
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            if _is_zombie(pid):
                continue  # already dead — init will reap it
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except (ProcessLookupError, OSError):
                pass
        if not alive:
            return
        time.sleep(0.05)
    for pid in pids:  # stuck — force it
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
# benchmark core
# ---------------------------------------------------------------------------
def _pct(sorted_lat: list[float], p: float) -> float:
    if not sorted_lat:
        return float("nan")
    idx = min(len(sorted_lat) - 1, max(0, int(round((p / 100.0) * (len(sorted_lat) - 1)))))
    return sorted_lat[idx]


def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:.2f}"


async def _echo_round(ws: _WSClient, n: int, timeout: float) -> float | None:
    token = f"{_TOKEN_PREFIX}{n}\r"
    await ws.send_text(f"echo {token}")
    start = time.monotonic()
    buf = b""
    while time.monotonic() - start < timeout:
        payload = await ws.recv_payload(timeout=timeout - (time.monotonic() - start))
        if payload is None:
            return None
        buf += payload
        if token.encode() in buf:
            return time.monotonic() - start
    return None


async def _run(port: int, rounds: int, warmup: int, burst: int,
               self_hosted: bool, proj_root: str) -> dict:
    # session setup
    status, config = await _http("GET", port, "/api/config")
    if status != 200:
        raise RuntimeError(f"/api/config failed with HTTP {status} — is a webpty server on port {port}?")
    cwd = config.get("projectsRoot") or proj_root
    status, sess = await _http("POST", port, "/api/sessions",
                               {"cwd": cwd, "tool": "bash", "name": "bench-ws"})
    if status != 201:
        raise RuntimeError(f"session create failed HTTP {status}: {sess}")
    sid = sess["id"]
    status, _ = await _http("POST", port, f"/api/sessions/{sid}/start", {})
    if status != 200:
        raise RuntimeError(f"session start failed HTTP {status}")

    ws = await _ws_connect(port, f"/ws/sessions/{sid}")
    try:
        # drain any startup banner
        try:
            await ws.recv_payload(timeout=0.5)
        except Exception:  # noqa: BLE001
            pass

        latencies: list[float] = []
        for i in range(warmup + rounds):
            lat = await _echo_round(ws, i, timeout=5.0)
            if lat is None:
                raise RuntimeError(f"echo round {i} timed out / connection dropped")
            if i >= warmup:
                latencies.append(lat)

        burst_rate = None
        if burst > 0:
            start = time.monotonic()
            sent = 0
            for i in range(burst):
                await ws.send_text(f"echo {_TOKEN_PREFIX}burst{i}\r")
                sent += 1
            # read until the last token's echo arrives
            last = f"{_TOKEN_PREFIX}burst{burst - 1}".encode()
            buf = b""
            while time.monotonic() - start < 20.0:
                payload = await ws.recv_payload(timeout=5.0)
                if payload is None:
                    break
                buf += payload
                if last in buf:
                    break
            elapsed = time.monotonic() - start
            burst_rate = sent / elapsed if elapsed > 0 else 0.0

        return {
            "port": port,
            "mode": "self-hosted" if self_hosted else "existing-server",
            "sid": sid,
            "rounds": rounds,
            "latency_ms": {
                "p50": _pct(sorted(latencies), 50),
                "mean": sum(latencies) / len(latencies),
                "p95": _pct(sorted(latencies), 95),
                "p99": _pct(sorted(latencies), 99),
                "max": max(latencies),
                "min": min(latencies),
            },
            "burst_msgs_per_s": burst_rate,
            "latency_samples": [round(x * 1000, 3) for x in latencies],
        }
    finally:
        await ws.close()


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="webpty WS echo latency (p95) + server memory peak benchmark")
    ap.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT,
                    help=f"port to test (default {DEFAULT_PORT})")
    ap.add_argument("--rounds", type=int, default=50)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--burst", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-self-host", action="store_true")
    args = ap.parse_args()

    port = args.port
    info: dict = {"python": sys.version.split()[0], "platform": sys.platform}

    # probe for an existing server
    try:
        status, _ = await _http("GET", port, "/api/config")
        existing = status == 200
    except (OSError, asyncio.TimeoutError):
        existing = False

    self_hosted = False
    proc: subprocess.Popen | None = None
    proj_root = ""
    tmp_dirs: list[str] = []
    if not existing:
        if args.no_self_host:
            print(f"no webpty server on port {port} and --no-self-host given; aborting",
                  file=sys.stderr)
            return 2
        # launch a throwaway instance
        proj_root = tempfile.mkdtemp(prefix="webpty-bench-")
        data_dir = tempfile.mkdtemp(prefix="webpty-bench-data-")
        tmp_dirs = [proj_root, data_dir]
        os.makedirs(os.path.join(proj_root, "p"), exist_ok=True)
        # I-1: the server reads bindHost from config.json (not the env), so
        # pre-write a config that pins the throwaway instance to loopback.
        # A fresh data dir has no authToken → gate "none"; without this the
        # instance would briefly listen on 0.0.0.0 with no auth.
        with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"bindHost": "127.0.0.1"}, f)
        env = dict(os.environ)
        env.update({
            "WEBPTY_DATA_DIR": data_dir,
            "WEBPTY_PROJECTS_ROOT": proj_root,
            "WEBPTY_PORT": str(port),
            "WEBPTY_BIND_HOST": "127.0.0.1",
        })
        proc = subprocess.Popen(
            [sys.executable, _SERVER],
            cwd=_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                status, _ = await _http("GET", port, "/api/config")
                if status == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        else:
            proc.kill()
            print("throwaway server did not come up; aborting", file=sys.stderr)
            return 2
        self_hosted = True

    # memory peak: sample RSS DURING the benchmark, while echo bursts are in
    # flight — that is when the server is at its heaviest — instead of after
    # the work has finished.
    pid = proc.pid if proc is not None else _pid_listening_on(port)
    peak_holder: dict[str, int | None] = {"kb": None}
    stop_sampling = asyncio.Event()

    async def _sample_loop() -> None:
        while not stop_sampling.is_set():
            if pid is not None:
                rss = _rss_kb(pid)
                if rss is not None:
                    peak_holder["kb"] = rss if peak_holder["kb"] is None \
                        else max(peak_holder["kb"], rss)
            try:
                await asyncio.wait_for(stop_sampling.wait(), 0.05)
            except asyncio.TimeoutError:
                pass

    sampler = asyncio.create_task(_sample_loop())
    results: dict | None = None
    try:
        results = await _run(port, args.rounds, args.warmup, args.burst,
                             self_hosted, proj_root)
    finally:
        stop_sampling.set()
        try:
            await sampler
        except Exception:  # noqa: BLE001
            pass
        # Cleanup: on an existing server, stop exactly the session we created
        # (never a same-named one the user may already have). On a throwaway
        # instance the whole data dir is removed below, so no stop is needed.
        if not self_hosted and results is not None and results.get("sid"):
            try:
                await _http("POST", port,
                            f"/api/sessions/{results['sid']}/stop", {})
            except Exception:  # noqa: BLE001
                pass

    if results is None:
        print("benchmark failed — see traceback above", file=sys.stderr)
        return 1

    peak_kb = peak_holder["kb"]
    if proc is not None:
        # I-2: the pty-host daemon is detach-spawned by the server and would
        # otherwise be left behind as an orphan (ppid=1). Capture the
        # server's direct children — the pty-host it spawned — while the
        # server is still alive, then TERM the server first: while it is
        # alive its host monitor re-spawns the pty-host the moment we TERM
        # it. With the server dead there is no respawner left, so the
        # captured pty-host can be safely TERM'd (its SIGTERM handler kills
        # its PTY sessions and unlinks its socket), then the data dir
        # removed. Using /proc parent-child instead of a global pty_host.py
        # cmdline scan means we never touch pty-hosts spawned by other
        # webpty servers on the host.
        host_pids = _children_pids(proc.pid)
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), 3)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            proc.kill()
        _terminate_pids(host_pids)
    for d in tmp_dirs:
        shutil.rmtree(d, ignore_errors=True)

    info.update(results)
    info["mem_peak_mib"] = (round(peak_kb / 1024, 1) if peak_kb is not None else None)

    if args.json:
        print(json.dumps(info))
        return 0

    lat = results["latency_ms"]
    print("webpty WS throughput bench")
    print(f"  server       : {info['mode']} on port {port} (python {info['python']}, {info['platform']})")
    print(f"  rounds       : {results['rounds']} echo round-trips (after {args.warmup} warm-up)")
    print(f"  latency      : min {_fmt_ms(lat['min'])} ms | p50 {_fmt_ms(lat['p50'])} ms | "
          f"mean {_fmt_ms(lat['mean'])} ms | p95 {_fmt_ms(lat['p95'])} ms | "
          f"p99 {_fmt_ms(lat['p99'])} ms | max {_fmt_ms(lat['max'])} ms")
    if results["burst_msgs_per_s"]:
        print(f"  burst {args.burst} echoes: {results['burst_msgs_per_s']:.1f} msgs/s")
    mem_str = (f"{info['mem_peak_mib']} MiB" if info["mem_peak_mib"] is not None else "n/a")
    print(f"  mem peak     : {mem_str} (VmRSS of server process)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
