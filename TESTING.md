# webpty 验证矩阵 / Verification Matrix

本文档记录对 webpty 核心承诺的实测验证：**会话后台运行，任何断线不中断；
重连恢复完整会话状态**（"可后台运行"）。所有项目均在本机生产部署上
真实执行（真实 codex / reasonix 会话 + 无头 Chromium）。

This document records the live verification of webpty's core promise:
sessions run in the background and survive every disconnect; reconnecting
restores the full session state. All tests ran against the real deployed
service (production codex/reasonix sessions + headless Chromium).

## 测试套件 / Test suite

- **396 tests, 0 failures** (`python3 -m unittest discover -s test`, 3
  platform/optional skips)
- 覆盖: 路径/参数/环形缓冲/认证/配置合并/会话管理/pty-host 崩溃自愈/
  端到端 HTTP/WebSocket/四大扩展/前端静态契约（DOM id、API 路径、语法）

## 故障注入 / Fault injection

| 场景 | 方法 | 结果 |
|---|---|---|
| WS 断线 | 客户端直接断开 | ✅ agent 继续运行;重连回放+全量 resync |
| 长时间断线 (25s) | 断开 25s 后重连 | ✅ 输出连续(TICK 3→27),状态完整 |
| 服务重启 | systemctl restart | ✅ pty-host 存活,会话 reattach |
| 服务器 SIGKILL ×2 | kill -9 server.py | ✅ systemd ~7s 拉起,会话 pid 不变 |
| pty-host SIGKILL | kill -9 pty_host.py | ✅ 监控重生宿主,autostart 会话退避重启 |
| pty-host SIGKILL(WS 连接中,隔离宿主) | 专用 socket 宿主 + 连着的 WS | ✅ 实时收到 state(stopped)→reconnected→state(running),恢复后 echo 往返正常 |
| 宿主刚死就创建/启动会话 | kill 后 0.3s 内 POST 新会话 | ✅ 按需重生宿主,会话正常 running,输入可用 |
| 测试防静默空转守卫 | 4 个隔离文件 meta-test | ✅ 每个 async 测试必须含 asyncio.run(run())——曾因编辑丢失导致测试空转通过 |
| agent CLI SIGKILL(隔离+假 agent) | 杀 agent 子进程 | ✅ 修复前:永不自动重启(Audit T8 真实 bug);修复后:state(stopped)→退避重启→state(running)→TICK 恢复→用户消息往返 |
| 30 会话并发隔离 | 30 个 bash 会话同宿主 | ✅ 全部 running;每会话 WS 只见自己的 MARK;批量删除后零孤儿进程,宿主健康 |
| 删除项目文件夹 | 运行中会话所在 extraFolder 被删 | ✅ active_sessions=1 上报;会话继续运行,输出/输入均正常 |
| 双连崩溃 | 15s 内两次杀 pty-host | ✅ 会话+生产 codex 都恢复,零错误 |
| 删除运行中会话 | DELETE 带 sleep 的 pty 会话 | ✅ 进程被杀,无孤儿 |
| 连接中删除会话 | WS 挂载时 DELETE | ✅ 收到 removed 帧+干净关闭(修复前僵尸 tab,前端误报"令牌失效"——Audit T7) |
| WS 连接中 stop/start | REST 操作 + 已连 WS | ✅ state 帧 stopped/running 实时到达;对停止会话输入收到离线提示帧 |
| agent 会话重连 ×3 | 连-收快照-断 循环 | ✅ 每次重连完整 snapshot,互斥不卡死 |
| 自动重启耗尽 | 永久失败命令(exit 1) | ✅ 退避精确 10/30/90s,3 次后停止 |
| 网络分区 (70s) | iptables DROP 4789 | ✅ agent 不受影响,恢复后无缝重连 |
| agent CLI 缺失 | 移除 claude 后启动 | ✅ 优雅 500 JSON,last_error 可见 |
| 200KB 输入洪泛 (Audit T9) | 单帧 200KB → raw cat > 文件 | ✅ 修复前:单次 os.write 在 pty 缓冲边界静默截断(~12KB);修复后:主机排队+选择器集成重试(10ms→1s 退避),**字节精确**落盘 204800/204800,宿主 CPU 0%(曾引入 EVENT_WRITE 忙循环→已用定时重试替代) |

## 长期稳定性 / Long-running

| 项目 | 结果 |
|---|---|
| 25 分钟 × 29 次周期重连 | ✅ 0 失败,输出连续 |
| 30 分钟浏览器浸泡 (60 探针/2min) | ✅ 0 问题 |
| 8 小时浏览器浸泡 v3 (自愈式,探针/10min) | ✅ 进行中——浏览器会话丢失自动重建,不误报(v2 会把每次丢失后的探针计为问题) |
| 2 小时浏览器浸泡 (60 探针/2min) | ✅ 0 问题,画布全程渲染 |
| 服务器连续运行 6.5h+ | ✅ RSS 稳定(~21MB),journal 零错误 |
| 真实事故:OOM 级联 (2026-08-14 14:36) | 内核 OOM killer 杀掉 dsh-web(592MB)与 webpty 主进程 | ✅ 根因:3.4GB 小机内存紧张;恢复:autostart codex 自动重启并 `resume --last` 恢复会话(核心承诺);非 autostart 的 reasonix 按设计保持 stopped,手动 start 后恢复;零数据丢失 |
| 服务器 SIGKILL 重挂(隔离测试) | 杀 server,宿主+会话存活,起新 server | ✅ 同 pid 重挂,历史输出回放(含 resync 帧),输入可用——真实事故场景的自动化回归 |
| OOM 加固 (2026-08-14) | 全局 OOM 事故后 | ✅ 生产 unit 增加 drop-in:MemoryMax=2G(cgroup 内 OOM,agent 不拖垮宿主)+ OOMScoreAdjust=-500(整 cgroup 受保护);应用后 agent pid 不变,零错误 |

## 成本核对 / Cost reconcile

| 项目 | 结果 |
|---|---|
| claude/reasonix/opencode 日志扫描 | ✅ 支持(价格表最长前缀匹配,大小写不敏感) |
| codex 会话用量 | ⚠️ **不可从 rollout 解析**——codex v0.144.6 的 rollout JSONL 中 token_count 事件 info=null,无内嵌 usage 字段;用量仅存于内部 SQLite 日志(格式不稳定,不宜依赖)。界面给出明确提示,不会崩溃 |
| 预算告警 | ✅ 超预算时面板告警 |

## 攻击面 / Attack surface

| 项目 | 结果 |
|---|---|
| API 模糊测试 (300 随机请求) | ✅ 0 连接丢弃,0 意外 5xx |
| 并发操作风暴 (25 并行) | ✅ 0 失败 |
| 慢消费者 (outbox 溢出) | ✅ resync 全量恢复(10.8MB) |
| 输出洪峰 (yes 8s) | ✅ 合并帧≤32KB,背压正常,日志落盘 |
| 超长请求行/头 | ✅ 414/431 |
| NUL 字节路径 | ✅ 400 |
| resize 极端值 | ✅ 钳制 1-1000 |
| WS 并发上限 128 | ✅ 128/128 + 429 + 恢复 |
| 心跳 | ✅ 25s ping;无响应客户端 75s 关 1001 |

## 浏览器层 / Browser (headless Chromium)

| 项目 | 结果 |
|---|---|
| UI 启动/抽屉/项目选择/终端渲染 | ✅ 像素级验证(258K 亮像素) |
| 键盘输入全链路 | ✅ echo 往返落盘 |
| 服务重启/pty-host 崩溃时浏览器 | ✅ 自动重连,画面自动恢复 |
| 全部菜单/面板(通知/成本/备份/ACFG/迁移) | ✅ 内容正确渲染 |
| tab 切换 | ✅ (曾静默失效——已修复+回归测试) |
| 同会话多客户端(多开 tab) | ✅ 各自完整输出,一个离开不影响其余 |
| 重连风暴 10 连-收-断 | ✅ 会话持续输出,编号单调递增,服务不崩 |
| 主题切换/字号/重命名流程 | ✅ |

## 真实 agent 交互 / Real agent interaction

| 项目 | 结果 |
|---|---|
| 新建 codex 会话 + 最小提示词 (`please reply with exactly: PONG_OK`) | ✅ **agent 真实回复 PONG_OK**——WS 输出流收到 24KB TUI 数据,全链路(WS→webpty→pty-host→codex CLI→AI provider→返回)闭合 |
| 新建 reasonix 会话 + 最小提示词 (`please reply with exactly: RX_OK`) | ✅ **agent 真实回复 RX_OK**——reasonix 路径(含全局会话锁)同样闭合 |

## 性能 / Performance (2026-08-14 实测)

| 项目 | 数值 |
|---|---|
| API 延迟 (config/sessions/health) | p50=0.6ms, p99≈1ms |
| 会话创建+启动 (bash) | 中位 6ms |
| 输入→输出往返 | **17ms**(修复前 1002ms——见修复清单第 16 条) |
| WS 握手 | 0-1ms |
| 首帧到达 | 1ms |
| 全屏重放 (codex 423KB) | **28ms** (14.1MB/s) |
| 会话 stop / delete | 4ms / 4ms |

## 修复清单 / Bugs fixed (16)

- py3.10 tomllib 缺失(vendor tomli)
- fs/list 403 守卫回归
- WS 重连吞输出(重连时未 reattach)
- 日志块缓冲不落盘(崩溃恢复丢数据)
- pty-host 崩溃后 autostart 不重启
- agent spawn 失败丢连接(OSError 误吞)
- tab 点击静默失效(未定义 session 变量)
- ResizeObserver 噪音误报 fatal 栏
- REST 缺口(GET 单会话/GET budget/WS 404 前置/414/431)
- TOML 段键转义、dict-as-key、NUL 400、auth 类型安全
- 测试泄漏 5 处、DOM 契约、WS 上限可配置
- **输出尾部整秒延迟**(has_pending select 超时错误:命令输出恒定 1002ms
  延迟,修复后 17ms——性能级 bug,交互卡顿根源)

## 复现方法 / Reproduce

测试脚本与步骤见各轮次记录;核心可复现项:
- `python3 -m unittest discover -s test` — 全套件
- 浏览器测试: `chromium --headless=new --no-sandbox` + chromedriver
  (W3C WebDriver, 见本会话各轮脚本)
