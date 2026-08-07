# webpty 深度优化设计 — 效率 / 速度 / 稳定性 / 兼容性

日期：2026-08-07
状态：设计中（brainstorming → 待用户审阅）

## 1. 背景与目标

webpty 已完成 Node/npm → Python 标准库迁移（v0.0.1，10 模块 2678 行，108 测试）。
本次以 **效率、速度、稳定性、兼容性** 四大核心目标均衡推进，做深度优化。

用户已确认的关键决策：
- 优化范围：四大目标均衡推进
- 兼容性：全平台（含 Windows），**接受 pywinpty 依赖**（POSIX 仍用标准库 pty）
- 性能重点：**交互流畅度**（PTY 输出转发的延迟与帧开销）
- 稳定性重点：**会话生命周期**（pty-host 崩溃恢复、重连、僵尸清理）

## 2. 现状问题清单（摸底确认）

### 2.1 速度/流畅度（P1）
- **P1-1 `ws.py` 每帧 task 风暴**：`send_bytes_async`/`send_text_async` 对每个输出 chunk
  都 `create_task(self.drain())`。高输出（如 `git log`、`ls -R`、agent 滚动）时每帧一个
  task，事件循环被 task 创建/调度开销拖垮，且多个 drain 并发可能导致写入交错。
- **P1-2 PTY 输出单帧转发**：`pty_host.py` 每 `os.read`（65536 上限）就 base64+JSON+广播
  一次。小 chunk 高频到达时帧率过高；反之大输出时单帧过大导致 WS 缓冲膨胀。
- **P1-3 WS 发送无背压**：`_send_frame` 只 `writer.write()`，从不检查 `transport` 缓冲
  水位。慢客户端会无限堆积内存。

### 2.2 效率（P2）
- **P2-1 重复序列化**：`pty_host` 每 chunk 独立 `json.dumps`+`base64`，可批量。
- **P2-2 静态资源未压缩**：xterm.js 289KB 未 gzip，`public/vendor` 已 immutable 缓存但
  首次拉取仍大。HTTP 层无 `Accept-Encoding` 处理。
- **P2-3 `recent_output` 每次全量快照**：WS 重连时 `snapshot()` 拷贝整个 128KB 缓冲，
  对每连接一次可接受，但应避免频繁调用。

### 2.3 稳定性（P1）
- **P3-1 pty-host 崩溃无自愈**：`_on_host_disconnect` 只标记会话 stopped，不重启 host、
  不重连。pty-host 进程死亡（OOM/误杀）后所有 PTY 会话永久丢失。
- **P3-2 僵尸进程回收**：`_reap_children` 存在但仅在 select 循环中轮询；host 崩溃后
  遗留的 PTY 子进程无人回收。
- **P3-3 WS 断线重连**：`_ws_session` 结束时 recent buffer 有数据但无状态同步；agent
  会话重连后 transcript 依赖 `snapshot`，PTY 会话重连仅重放 recent——二者行为不一致。
- **P3-4 异常吞噬**：多处 `except Exception: pass`（pty_host client、server 回调）导致
  故障不可见，难以诊断。

### 2.4 兼容性（P1）
- **P4-1 Windows 无 PTY**：`pty_host.py` 用 `pty.fork`（POSIX only），Windows 上
  `_ensure_host_running` 直接抛错。需 pywinpty 后端。
- **P4-2 部署脚本 POSIX 假定**：`install.sh` 用 systemd；Windows 需备用部署路径。
- **P4-3 `ws.py` 已实现 RFC 6455 全帧类型**：检查兼容性（分片帧不支持，客户端不分片
  则无碍；但需确认 xterm 前端二进制帧路径）。

## 3. 方案（已选 A：定向优化）

### 3.1 速度/流畅度 — WS 输出管线重设计

**设计：per-connection 写队列 + 合并转发 + 背压**

```
pty_host ──(JSON行)──▶ server._ws_session
                          │
                          ▼
              ┌─────────────────────┐
              │  OutboxQueue (asyncio.Queue)  │  单消费者 task 持续 drain
              │  • put_nowait 合并小 chunk     │  • drain 失败→标记断线→关闭
              └─────────────────────┘
```

- **`ws.py` 新增 `Outbox`**：内部 `asyncio.Queue`（容量如 1024 帧）+ 单后台 writer task。
  调用方 `send_bytes()`/`send_text()` 变为同步入队（`put_nowait`），满则丢弃最旧帧
  （drop-oldest，保证实时性优先于完整性——终端输出允许丢帧）或置 `overflow` 标记。
  **消除每帧 create_task**。
- **合并小 chunk**：pty_host 侧累积 ≤16ms 或 ≤32KB 的输出合并成单帧广播；server 侧
  对 ≤4KB 的连续二进制帧合并后再入队。两者择一实现（倾向 server 侧合并，改动小）。
- **背压**：writer task 里 `await drain()`，若 `transport.is_closing()` 或写缓冲超过
  阈值（如 8MB）则丢弃新帧并记录 `dropped` 计数；前端可选显示"输出被截断"提示。

**验收**：基准测试 — 100MB `yes` 输出到 PTY，WS 端接收延迟 p95 < 200ms（对比优化前）；
进程内存不随输出无限增长（有界队列）。

### 3.2 效率 — 静态资源压缩 + 序列化优化

- **HTTP gzip**：`server.py` 对 `Accept-Encoding: gzip` 的响应（静态资源、`/api/config`
  等 JSON）做 gzip。`Content-Length` 按压缩后字节。仅压缩 >1KB 响应避免小响应开销。
- **批量序列化**：pty_host 对合并后的输出批量 `base64`+`json.dumps` 一次（合并天然
  复用此收益）。
- **`recent_output` 复用**：WS 重连时 `snapshot()` 结果缓存 30s，避免同一会话连续重连
  重复拷贝。

### 3.3 稳定性 — 会话生命周期加固

- **P3-1 pty-host 自愈**：`SessionManager` 增加 `_host_monitor` 后台 task：每 2s 检测
  `host._connected`；断线时自动 `host.connect()` 重连 + `host.list()` 重建
  `host_sessions` + 对 running 会话 `_reattach`。重连成功后向所有 WS 客户端推送
  `{"type":"reconnected"}` 事件。
- **P3-2 僵尸清理**：`pty_host.py` `_reap_children` 已存在；补：host 启动时 `waitpid(-1,
  WNOHANG)` 清残留；server 侧对"host 崩溃后仍标 running 的会话"标记
  `state=unknown`（可重启），避免永久假 running。
- **P3-3 重连一致性**：PTY 会话重连统一走 `recent_output` 重放（现有）；agent 会话
  重连走 transcript snapshot（现有）——补文档明确行为差异；两者都推送 `state` 事件。
- **P3-4 可见性**：将 pty_host_client 与 server 的裸 `except: pass` 改为 `except
  Exception as e: log + 计数`；新增 `_last_error` 字段暴露到 `/api/config` 诊断块。

### 3.4 兼容性 — Windows 支持

- **`pty_host.py` 平台分派**：`main()` 检测 `os.name == "nt"` 时导入
  `pty_host_windows.py`（pywinpty 后端），否则走现有 stdlib pty 逻辑。统一协议层
  （JSON 行 + base64）不变，`pty_host_client` 无需改动。
- **`pty_host_windows.py`**：
  - 依赖 `pywinpty`（`pip install pywinpty`，仅 Windows 安装；`install.sh` 加
    `[ -n "$WINDOWS" ]` 分支或 README 说明 `pip install -r requirements-windows.txt`）。
  - 用 `winpty.PtyProcess.start(cmd, cwd, cols, rows, env)` 创建伪终端；
    `read()`/`write()` 同步封装为 selectors/线程事件。
  - 注意：pywinpty 无原生异步 read，需后台线程 + `queue.Queue` 桥接到主循环
    （与 stdlib 版 selectors 模型对齐）。
- **部署**：`install.sh` 增加 Windows 分支——检测无 systemd 时用
  `pythonw`/`nssm` 或提示手动 `python src/server.py`；README 补 Windows 安装节。
- **降级路径**：Windows 无 pywinpty 时启动即打印明确错误（不再静默崩），
  `/api/config` 暴露 `platform` 与 `ptyBackend` 供前端诊断。

### 3.5 测试计划

- **单元**：
  - `test_ws.py`：Outbox 合并/丢弃/背压；帧编码（<126/126/127 长度、掩码、分片拒绝）。
  - `test_pty_host.py`：合并转发（模拟 100 小 chunk → ≤N 帧）；`_reap_children` 僵尸回收。
  - `test_session_manager.py`：host 断线→自动重连→会话 reattach；重连事件推送。
- **集成**：
  - `test_server.py` 增：gzip 响应（Accept-Encoding 头）；大输出流式（`yes | head`）内存
    有界断言；WS 断线重连后 recent 重放。
  - Windows 测试标 `@unittest.skipUnless(os.name == "nt")`（CI 无 Windows 则跳过）。
- **基准**（非 gate，记录用）：`bench/ws_throughput.py` — 输出延迟 p95、内存峰值。

## 4. 里程碑（对应 writing-plans 的实施顺序）

1. **M1 速度**：Outbox + 合并转发 + 背压（P1-1/2/3）
2. **M2 效率**：gzip + 批量序列化 + recent 缓存（P2-1/2/3）
3. **M3 稳定性**：host 自愈监控 + 僵尸清理 + 重连一致 + 错误可见（P3-1~4）
4. **M4 兼容性**：Windows pywinpty 后端 + 部署适配（P4-1/2/3）
5. **M5 收尾**：全量测试 + 基准 + 文档 + 提交

## 5. 风险与取舍

- **丢帧策略**：实时性优先（drop-oldest）牺牲完整性——终端场景可接受；agent 会话
  transcript 不受影响（走结构化 JSON，非原始字节）。
- **pywinpty 线程桥接**：Windows 输出经线程→queue→主循环，延迟略高于 POSIX 直接
  selectors；接受（Windows 非首选运行环境）。
- **gzip CPU 开销**：仅压缩 >1KB 响应，避免小请求开销；静态资源压缩一次缓存（进程内
  dict 缓存压缩结果）。
- **不做**：uvloop 替换、共享内存环形队列、跨进程会话热迁移（YAGNI，收益/风险不匹配）。
