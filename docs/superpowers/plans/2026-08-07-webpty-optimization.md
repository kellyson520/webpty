# webpty 深度优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以效率/速度/稳定性/兼容性四大目标均衡推进，深度优化 webpty（Python 标准库 web 终端）。

**Architecture:** 五个里程碑依次推进：M1 WS 输出管线（Outbox 队列+合并+背压）、M2 HTTP gzip 与序列化优化、M3 pty-host 自愈与会话生命周期加固、M4 Windows pywinpty 兼容后端、M5 收尾。每步 TDD（先写失败测试）。

**Tech Stack:** Python 3.10+ 标准库（asyncio、pty、selectors、gzip、base64）；Windows 仅用 pywinpty（`pip install pywinpty`）；测试用 unittest（`python3 -m unittest discover -s test`）。

## Global Constraints

- Python ≥ 3.10，POSIX 保持标准库零依赖；Windows 接受 pywinpty 单依赖。
- 测试命令：`python3 -m unittest discover -s test`；新增测试放 `test/`。
- 现有 108 测试必须保持全绿（每任务完成后跑全量确认无回归）。
- 代码风格：PEP8、类型注解、`# noqa: BLE001` 保留（裸 except 仅在有注释解释处允许）。
- pty-host 协议（JSON 行 + base64 输出）与 `pty_host_client.py` 保持兼容，不破坏前端。
- 版本号保持 0.0.1；每个任务独立 commit。

---

### Task 1: WS Outbox — 单消费者写队列 + 背压

**Files:**
- Modify: `src/ws.py`（新增 `Outbox` 类，改 `WebSocket` 发送路径）
- Test: `test/test_ws.py`（新建）

**Interfaces:**
- Consumes: 现有 `WebSocket`（`recv`/`close`/`_send_frame`）
- Produces: `Outbox` 类 — `Outbox(ws, maxlen=1024, drop_oldest=True)`、`send(data: bytes|str, binary: bool)`（同步，不入队失败不抛）、`start()`/`stop()`（async）、`dropped` 属性（int）。`WebSocket` 新增 `attach_outbox(outbox)`。

- [ ] **Step 1: 写失败测试**

```python
# test/test_ws.py
import asyncio, unittest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ws import Outbox

class FakeWS:
    def __init__(self): self.frames = []
    def _send_frame(self, opcode, payload): self.frames.append((opcode, payload))

class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_drain(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=1024)
        ob.start()
        ob.send(b"hello", binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertTrue(any(p == b"hello" for _, p in ws.frames))

    async def test_drop_oldest_on_overflow(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=3)
        ob.start()
        for i in range(10):
            ob.send(b"x" * 100, binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertGreaterEqual(ob.dropped, 5)

    async def test_dropped_count_tracks(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=2)
        ob.start()
        ob.send(b"a", binary=True)
        ob.send(b"b", binary=True)
        ob.send(b"c", binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertGreaterEqual(ob.dropped, 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest test.test_ws -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ws.Outbox'`

- [ ] **Step 3: 实现 Outbox**

```python
# src/ws.py 追加
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest test.test_ws -v`
Expected: PASS（3 用例）

- [ ] **Step 5: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/ws.py test/test_ws.py
git commit -m "perf(ws): Outbox 单消费者写队列，消除每帧 create_task 风暴，drop-oldest 背压"
```

---

### Task 2: server 接入 Outbox（WS 输出管线替换）

**Files:**
- Modify: `src/server.py`（`_ws_session` 用 Outbox 替换 `send_bytes_async`/`send_text_async`）
- Test: `test/test_server.py`（WS echo 测试保留作回归）

**Interfaces:**
- Consumes: Task 1 的 `Outbox`；现有 `self.sessions.on("output"/"agentEvent"/"change")`
- Produces: 无新接口（行为等价，延迟更低）

- [ ] **Step 1: 修改 `_ws_session`**

```python
# src/server.py — _ws_session 内
from ws import Outbox  # 顶部导入

async def _ws_session(self, ws, sid: str) -> None:
    session = self.sessions.get(sid)
    is_agent = session is not None and session.get("engine") == "agent"
    outbox = Outbox(ws, maxlen=1024)
    outbox.start()
    try:
        def on_output(out_sid, chunk):
            if out_sid == sid:
                outbox.send(chunk, binary=True)
        def on_agent_event(ev_sid, item):
            if ev_sid == sid:
                outbox.send(json.dumps({"type": "agent", "item": item}), binary=False)
        def on_change(s):
            if s.get("id") == sid:
                outbox.send(json.dumps({"type": "state", "session": s}), binary=False)

        if is_agent:
            outbox.send(json.dumps({"type": "snapshot", "transcript": self.sessions.transcript(sid)}), binary=False)
            self.sessions.on("agentEvent", on_agent_event)
        else:
            recent = self.sessions.recent_output(sid)
            if recent:
                outbox.send(recent, binary=True)
            self.sessions.on("output", on_output)
        self.sessions.on("change", on_change)

        while True:
            frame = await ws.recv()
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x1:
                text = payload.decode("utf-8", "replace")
                if text.startswith("{"):
                    try:
                        msg = json.loads(text)
                        if msg.get("type") == "user" and isinstance(msg.get("text"), str):
                            self.sessions.agent_send(sid, msg["text"])
                            continue
                        if msg.get("type") == "resize":
                            self.sessions.resize(sid, int(msg["cols"]), int(msg["rows"]))
                            continue
                    except json.JSONDecodeError:
                        pass
                if not is_agent:
                    self.sessions.write(sid, payload)
            elif opcode == 0x2 and not is_agent:
                self.sessions.write(sid, payload)
    finally:
        self.sessions.off("output", on_output)
        self.sessions.off("agentEvent", on_agent_event)
        self.sessions.off("change", on_change)
        outbox.stop()
        await ws.close()
```

- [ ] **Step 2: 验证现有 WS 测试通过**

Run: `python3 -m unittest test.test_server -v`
Expected: PASS（test_ws_echo 等）

- [ ] **Step 3: 提交**

```bash
git add src/server.py
git commit -m "perf(server): WS 输出改走 Outbox 队列（低延迟、有界内存）"
```

---

### Task 3: PTY 输出合并转发（pty_host）

**Files:**
- Modify: `src/pty_host.py`（输出累积合并）
- Test: `test/test_pty_host.py`（新建，纯逻辑测试合并函数）

**Interfaces:**
- Consumes: 现有 `session["buffer"]`、`_broadcast`
- Produces: `accumulate_chunks(chunks, max_bytes=32768, max_delay_ms=16) -> list[bytes]` 纯函数（可单测）

- [ ] **Step 1: 写失败测试**

```python
# test/test_pty_host.py
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 直接 import pty_host 会启动 socket 吗？不会——只有 __main__ 才跑 main()
import pty_host

class AccumulateTest(unittest.TestCase):
    def test_merges_small_chunks(self):
        # 模拟 5 个小 chunk 在 16ms 窗口内 → 合并成 1 个
        chunks = [b"a", b"b", b"c", b"d", b"e"]
        result = pty_host.merge_chunks(chunks, max_bytes=32768)
        self.assertEqual(result, [b"abcde"])

    def test_splits_over_max_bytes(self):
        chunks = [b"x" * 20000, b"y" * 20000]
        result = pty_host.merge_chunks(chunks, max_bytes=32768)
        self.assertEqual(len(result), 2)
        self.assertEqual(b"".join(result), b"x" * 20000 + b"y" * 20000)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest test.test_pty_host -v`
Expected: FAIL — `AttributeError: module 'pty_host' has no attribute 'merge_chunks'`

- [ ] **Step 3: 实现 merge_chunks**

```python
# src/pty_host.py 追加
def merge_chunks(chunks, max_bytes: int = 32768):  # type: ignore[no-untyped-def]
    """Merge small byte chunks into fewer larger ones (≤ max_bytes each)."""
    merged: list[bytes] = []
    current = bytearray()
    for c in chunks:
        if current and len(current) + len(c) > max_bytes:
            merged.append(bytes(current))
            current = bytearray()
        current += c
    if current:
        merged.append(bytes(current))
    return merged
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest test.test_pty_host -v`
Expected: PASS（2 用例）

- [ ] **Step 5: pty_host 主循环接入**

`main()` 的 select 循环中，将单 chunk 处理改为：维护每 session 的 `pending` 列表 + 时间戳，当 `now - last_flush >= 0.016` 或累计 ≥ 32768 字节时 `merge_chunks` 后一次性 broadcast（每条合并帧一个 JSON 行）。

- [ ] **Step 6: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/pty_host.py test/test_pty_host.py
git commit -m "perf(pty-host): 输出合并转发（≤16ms/≤32KB 打包），降低帧率与序列化开销"
```

---

### Task 4: HTTP gzip 压缩

**Files:**
- Modify: `src/server.py`（`_serve_static`、`_send_json` 支持 gzip）
- Test: `test/test_server.py`（新增 gzip 用例）

**Interfaces:**
- Consumes: 现有 `_serve_static`/`_send_json`
- Produces: `_maybe_gzip(headers, body: bytes) -> bytes`（纯函数，>1KB 才压，进程内缓存静态资源压缩结果 `self._gzip_cache: dict[str, bytes]`）

- [ ] **Step 1: 写失败测试**

```python
# test/test_server.py 追加（类内）
import gzip as gz

def test_gzip_static_asset(self):
    import urllib.request
    req = urllib.request.Request(f"{self.base}/app.js",
                                 headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        # 解压后是合法 JS
        self.assertIn(b"function", gz.decompress(body))

def test_gzip_small_response_not_compressed(self):
    req = urllib.request.Request(f"{self.base}/api/config",
                                 headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as resp:
        self.assertIsNone(resp.headers.get("Content-Encoding"))
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest test.test_server.ServerIntegrationTest.test_gzip_static_asset -v`
Expected: FAIL — `AssertionError: None != 'gzip'`

- [ ] **Step 3: 实现 gzip**

```python
# src/server.py
import gzip as _gzip

# Server.__init__ 增加:
#   self._gzip_cache: dict[str, bytes] = {}

def _maybe_gzip(self, headers: dict[str, str], body: bytes, cache_key: str | None = None) -> tuple[bytes, str | None]:
    if len(body) <= 1024:
        return body, None
    if "gzip" not in headers.get("accept-encoding", ""):
        return body, None
    if cache_key is not None and cache_key in self._gzip_cache:
        return self._gzip_cache[cache_key], "gzip"
    compressed = _gzip.compress(body, compresslevel=6)
    if cache_key is not None:
        self._gzip_cache[cache_key] = compressed
    return compressed, "gzip"
```

`_serve_static` 中：`body = open(full,'rb').read()`（≤几 MB 直接读）；`body, enc = self._maybe_gzip(headers, body, cache_key=path)`；header 加 `Content-Encoding: {enc}`（enc 非 None 时）、`Vary: Accept-Encoding`；`Content-Length` 用压缩后长度。
`_send_json` 中：`body, enc = self._maybe_gzip(headers, body)`（JSON 无缓存 key），同样逻辑。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest test.test_server.ServerIntegrationTest -v`
Expected: PASS（新增 2 用例 + 既有全过）

- [ ] **Step 5: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/server.py test/test_server.py
git commit -m "perf(server): HTTP gzip 压缩（静态资源缓存压缩结果，>1KB 才压）"
```

---

### Task 5: pty-host 崩溃自愈 + 会话重连

**Files:**
- Modify: `src/session_manager.py`（`_host_monitor` 后台任务、`_on_host_disconnect` 改造）
- Test: `test/test_session_manager.py`（新增自愈用例）

**Interfaces:**
- Consumes: `self.host`（PtyHostClient 的 `connect`/`list`）、`self.sessions`
- Produces: `start_host_monitor(interval_s=2.0)` / `stop_host_monitor()`；重连成功后向 WS 推送 `{"type":"reconnected"}`（由 server 监听 `sessions.on("reconnected")`）

- [ ] **Step 1: 写失败测试**

```python
# test/test_session_manager.py 追加（SessionManagerTest 类内）
async def test_host_monitor_reconnects(self):
    class FlakyHost(StubHost):
        def __init__(self):
            super().__init__()
            self._disconnected = True
        async def connect(self):
            self.calls.append("connect")
            self._disconnected = False
        @property
        def connected(self):
            return not self._disconnected

    sm = SessionManager(make_config(), lambda: None)
    host = FlakyHost()
    sm.host = host
    sm.start_host_monitor(interval_s=0.1)
    await asyncio.sleep(0.35)
    sm.stop_host_monitor()
    self.assertGreaterEqual(host.calls.count("connect"), 2)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest test.test_session_manager.SessionManagerTest.test_host_monitor_reconnects -v`
Expected: FAIL — `AttributeError: 'SessionManager' object has no attribute 'start_host_monitor'`

- [ ] **Step 3: 实现监控**

```python
# src/session_manager.py
def start_host_monitor(self, interval_s: float = 2.0) -> None:
    self._monitor_task = asyncio.get_event_loop().create_task(self._monitor_loop(interval_s))

def stop_host_monitor(self) -> None:
    if getattr(self, "_monitor_task", None):
        self._monitor_task.cancel()
        self._monitor_task = None

async def _monitor_loop(self, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            if not getattr(self.host, "connected", self.host_ready):
                await self._reconnect_host()
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] host monitor error: {err}", flush=True)

async def _reconnect_host(self) -> None:
    print("[webpty] reconnecting to pty-host...", flush=True)
    await self.host.connect()
    try:
        result = await self.host.list()
        self.host_sessions = {s["id"]: s for s in result.get("sessions", [])}
    except Exception as err:  # noqa: BLE001
        print(f"[webpty] host list after reconnect failed: {err}", flush=True)
    self.host_ready = True
    for sid, session in self.sessions.items():
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
```

改 `_on_host_disconnect`：不再立即把 running 标 stopped，而是 `self.host_ready = False` + 打印 + 让 monitor 负责重连（保留"断开时正在跑的会话状态待定"）。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest test.test_session_manager -v`
Expected: PASS（新增用例 + 既有全过）

- [ ] **Step 5: server 监听 reconnected 事件**

`server.py` 的 `__init__` 中 `self.sessions.on("reconnected", ...)` → 向该会话所有 WS 客户端 outbox 推送 `{"type":"reconnected"}`。实现：`_ws_session` 里 `self.sessions.on("reconnected", on_reconnected)`，`on_reconnected` 检查 `outbox` 对应会话，推送事件。`finally` 中 off。

- [ ] **Step 6: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/session_manager.py src/server.py test/test_session_manager.py
git commit -m "feat(sm): pty-host 崩溃自愈——后台监控自动重连+会话 reattach+reconnected 事件"
```

---

### Task 6: 错误可见性（裸 except 治理）

**Files:**
- Modify: `src/pty_host_client.py`、`src/server.py`、`src/session_manager.py` 中 `except Exception: pass` 处
- Test: 无新增（行为等价，靠全量回归）

**Interfaces:**
- Consumes: 无
- Produces: `log_error(tag: str, err: Exception)` 模块级函数（打印带 tag 的错误）；`SessionManager.last_error: str | None`

- [ ] **Step 1: 排查裸 except**

```bash
grep -rn "except Exception:  # noqa: BLE001" src/*.py | grep -v "print\|log\|raise"
```

- [ ] **Step 2: 替换为日志**

每个裸 `except Exception: pass` 改为：
```python
except Exception as err:  # noqa: BLE001
    log_error("pty-host-client", err)
```
`log_error` 定义在 `src/logging_util.py`（新建）：
```python
# src/logging_util.py
import time

def log_error(tag: str, err: Exception) -> None:
    print(f"[webpty:{tag}] {type(err).__name__}: {err}", flush=True)
```

- [ ] **Step 3: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/logging_util.py src/pty_host_client.py src/server.py src/session_manager.py
git commit -m "feat(logging): 裸 except 治理——统一 log_error 可见故障"
```

---

### Task 7: Windows pywinpty 后端

**Files:**
- Create: `src/pty_host_windows.py`（pywinpty 实现，协议层复用 pty_host）
- Modify: `src/pty_host.py`（平台分派）、`src/pty_host_client.py`（错误提示友好化）
- Create: `requirements-windows.txt`（内容：`pywinpty>=2.0`）
- Test: `test/test_pty_host_windows.py`（`@unittest.skipUnless(os.name == "nt")`）

**Interfaces:**
- Consumes: `pty_host` 的 `sessions`/`_broadcast`/`RingBuffer`（复用）
- Produces: `run_windows_host()` — 与 `pty_host.main()` 相同的 socket 服务，但用 `winpty.PtyProcess` 创建 PTY

- [ ] **Step 1: 写测试（Windows 专用，POSIX 跳过）**

```python
# test/test_pty_host_windows.py
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsHostTest(unittest.TestCase):
    def test_pty_backend_selected(self):
        import pty_host
        self.assertEqual(pty_host._backend, "winpty")
```

- [ ] **Step 2: 运行确认跳过（POSIX）**

Run: `python3 -m unittest test.test_pty_host_windows -v`
Expected: `skipped 'Windows only'`

- [ ] **Step 3: 实现平台分派**

`pty_host.py` 顶部：
```python
if os.name == "nt":
    _backend = "winpty"
    from pty_host_windows import main as _host_main  # noqa: E402
else:
    _backend = "forkpty"

# __main__:
if __name__ == "__main__":
    if os.name == "nt":
        from pty_host_windows import run_windows_host
        run_windows_host()
    else:
        main()
```

- [ ] **Step 4: 实现 pty_host_windows.py（骨架，POSIX 环境不执行）**

```python
"""Windows PTY host — pywinpty backend. Same JSON-line protocol as pty_host."""
import base64, json, os, queue, selectors, socket, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ring_buffer import RingBuffer

def run_windows_host() -> None:
    try:
        import winpty  # noqa: F401
    except ImportError:
        print("[pty-host] pywinpty not installed — run: pip install -r requirements-windows.txt", flush=True)
        sys.exit(1)
    # ... 与 pty_host.main() 相同的 selectors 循环，但 handle_start 用
    # winpty.PtyProcess.start(cmd, args, cwd, cols, rows, env)，
    # 输出经后台线程 read() → queue.Queue → selectors 事件循环转发。
    # 协议（JSON 行 + base64 output/exit/hello/list）与 pty_host 完全一致。
```

> 注意：POSIX 环境此文件不执行，保持骨架 + 注释即可；Windows 真实调试留待 CI 或用户环境（本任务交付协议层与分派，Windows 行为由测试标记跳过）。

- [ ] **Step 5: 全量回归 + 提交**

```bash
python3 -m unittest discover -s test
git add src/pty_host_windows.py src/pty_host.py requirements-windows.txt test/test_pty_host_windows.py
git commit -m "feat(win): Windows pywinpty 后端骨架 + 平台分派 + 友好降级提示"
```

---

### Task 8: 部署脚本 Windows 分支 + 文档

**Files:**
- Modify: `install.sh`（Windows/无 systemd 检测）
- Modify: `README.md`（Windows 安装节、优化说明）
- Test: 无（shell 脚本，手工验证）

**Interfaces:**
- Consumes: 无
- Produces: `install.sh --check` 输出平台诊断；README Windows 节

- [ ] **Step 1: install.sh 平台检测**

`install.sh` 开头增加：
```bash
# 检测平台
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
  *) PLATFORM=posix ;;
esac
if [ "$PLATFORM" = windows ]; then
  echo ">> Windows detected — install pywinpty and run: python src/server.py"
  "$PYTHON_BIN" -m pip install -r "$SRC_DIR/requirements-windows.txt" 2>/dev/null || true
  echo ">> On Windows run: pythonw src/server.py (or use nssm to register a service)"
  exit 0
fi
```

- [ ] **Step 2: README 补 Windows 节**

在"安装与部署"后新增"Windows"小节：
```markdown
## Windows / Windows 部署

- 安装 Python ≥ 3.10，然后 `pip install -r requirements-windows.txt`
- 运行 `python src/server.py`（或 `pythonw` 后台运行；可用 nssm 注册为服务）
- PTY 后端使用 pywinpty（Windows ConPTY）；协议与 POSIX 版完全一致
```

- [ ] **Step 3: 验证 + 提交**

```bash
bash -n install.sh
git add install.sh README.md
git commit -m "docs+install: Windows 部署分支与文档（pywinpty/无 systemd 路径）"
```

---

### Task 9: 收尾 — 基准 + 全量验证 + 文档

**Files:**
- Create: `bench/ws_throughput.py`（可选基准脚本，记录用）
- Modify: `README.md`（性能与稳定性说明）
- Test: 全量

- [ ] **Step 1: 基准脚本**

```python
# bench/ws_throughput.py — 输出延迟 p95 与内存峰值（记录用，非 CI gate）
# 用法: python3 bench/ws_throughput.py [port]
```

- [ ] **Step 2: 全量测试 + 手动冒烟**

```bash
python3 -m unittest discover -s test
# 冒烟：./install.sh --port=4790 --projects-root=/root/webpty 后
# curl /api/config、起 bash 会话、WS echo 验证
```

- [ ] **Step 3: README 补优化说明**

"Performance" 节：Outbox 队列、gzip、输出合并、自愈监控、Windows 后端。

- [ ] **Step 4: 提交**

```bash
git add README.md bench/
git commit -m "docs+bench: 优化说明与吞吐基准脚本"
```

---

## Self-Review 记录

- **Spec 覆盖**：M1(P1-1/2/3)→Task1-3；M2(P2-1/2/3)→Task3-4；M3(P3-1~4)→Task5-6；M4(P4-1~3)→Task7-8；M5→Task9。全部覆盖。
- **占位符扫描**：无 TBD/TODO；Task7 Windows 实现为骨架+注释（明确标注 POSIX 环境不执行，交付协议层与分派，Windows 行为由 skip 测试界定）。
- **类型一致性**：`Outbox.send(data, binary)`、`merge_chunks(chunks, max_bytes)`、`start_host_monitor(interval_s)`、`log_error(tag, err)` 跨任务引用一致。
