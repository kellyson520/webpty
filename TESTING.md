# webpty 验证矩阵 / Verification Matrix

本文档记录对 webpty 核心承诺的实测验证：**会话后台运行，任何断线不中断；
重连恢复完整会话状态**（"可后台运行"）。所有项目均在本机生产部署上
真实执行（真实 codex / reasonix 会话 + 无头 Chromium）。

This document records the live verification of webpty's core promise:
sessions run in the background and survive every disconnect; reconnecting
restores the full session state. All tests ran against the real deployed
service (production codex/reasonix sessions + headless Chromium).

## 测试套件 / Test suite

- **373 tests, 0 failures** (`python3 -m unittest discover -s test`, 3
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
| 双连崩溃 | 15s 内两次杀 pty-host | ✅ 会话+生产 codex 都恢复,零错误 |
| 自动重启耗尽 | 永久失败命令(exit 1) | ✅ 退避精确 10/30/90s,3 次后停止 |
| 网络分区 (70s) | iptables DROP 4789 | ✅ agent 不受影响,恢复后无缝重连 |
| agent CLI 缺失 | 移除 claude 后启动 | ✅ 优雅 500 JSON,last_error 可见 |

## 长期稳定性 / Long-running

| 项目 | 结果 |
|---|---|
| 25 分钟 × 29 次周期重连 | ✅ 0 失败,输出连续 |
| 30 分钟浏览器浸泡 (60 探针/2min) | ✅ 0 问题 |
| 2 小时浏览器浸泡 (60 探针/2min) | ✅ 0 问题,画布全程渲染 |
| 服务器连续运行 6.5h+ | ✅ RSS 稳定(~21MB),journal 零错误 |

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
| 主题切换/字号/重命名流程 | ✅ |

## 真实 agent 交互 / Real agent interaction

| 项目 | 结果 |
|---|---|
| 新建 codex 会话 + 最小提示词 (`please reply with exactly: PONG_OK`) | ✅ **agent 真实回复 PONG_OK**——WS 输出流收到 24KB TUI 数据,全链路(WS→webpty→pty-host→codex CLI→AI provider→返回)闭合 |

## 修复清单 / Bugs fixed (15)

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

## 复现方法 / Reproduce

测试脚本与步骤见各轮次记录;核心可复现项:
- `python3 -m unittest discover -s test` — 全套件
- 浏览器测试: `chromium --headless=new --no-sandbox` + chromedriver
  (W3C WebDriver, 见本会话各轮脚本)
