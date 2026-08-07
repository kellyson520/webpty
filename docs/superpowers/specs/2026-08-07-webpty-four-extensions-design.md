# WebPty 四大合规扩展设计 — 通知 / 成本 / 备份 / 迁移

日期：2026-08-07
状态：设计中（brainstorming → 待用户审阅）
依据：`/mnt/WebPty_顶层架构宪章_·_最终完整版（含成本管理_通知_备份迁移）.txt`（最高优先级技术宪法）

## 1. 背景与目标

WebPty 宪章确立项目终身定位：**私有编程 Agent 集群的「运行时底座 + 资源成本运维平台」**。
五层强制分层架构（展示层→API/MCP→业务管理层→抽象适配层→PTY 内核层），
三大铁律（单一职责/内核零业务/向上扩展）。

本设计落地宪章"合法可拓展能力清单"中的四大扩展：
1. 任务状态通知系统（邮箱 + WebUI 通知中心）
2. Token & 成本管理系统（精细化计量/账单/风控）
3. 全自动配置备份系统（快照/加密/版本回溯）
4. 一键配置迁移 & 环境克隆（单机↔集群）

**铁律落实**：所有新能力仅落在业务管理层（第三层），PTY 内核层（第五层）零侵入；
session_manager 仅新增"事件钩子"（emit 事件，不承载业务逻辑）。

## 2. 用户已确认的关键决策

| 决策点 | 选择 |
|---|---|
| 实施范围 | 四大扩展全部纳入本轮 |
| 部署形态 | 单进程内扩展 + 集群预留（WorkerInterface 抽象） |
| 成本数据源 | 混合模式（实时解析 stream-json + 事后日志校对） |
| 通知渠道 | SMTP 邮件 + WebUI 通知中心 |
| 备份存储 | 本地快照 tar.gz + SHA256 校验 + 可选 AES-GCM 加密 + 轮转 |
| 统一存储 | SQLite（标准库 sqlite3，WAL 模式） |
| 备份加密 | 可选按需启用（检测到 pycryptodome 才加密，默认关，保持零依赖） |
| 迁移导入冲突 | 默认 merge（保留现有+新增），可指定 replace / dry-run |
| 成本价格表 | 内置常见模型默认价（claude/codex/reasonix/opencode）+ 用户可覆盖 |

## 3. 架构总览

```
┌─ 展示层（WebUI 新增 4 面板）────────────────────────┐
│  通知中心 | 成本账单 | 备份管理 | 迁移向导              │
└──────────────┬─────────────────────────────────────┘
┌─ API 层（新增 REST 端点）───────────────────────────┐
│  /api/notify/*  /api/cost/*  /api/backup/*  /api/migrate/* │
└──────────────┬─────────────────────────────────────┘
┌─ 业务管理层（唯一扩展层，新增 5 模块）───────────────┐
│  notifier.py  cost_tracker.py  backup.py  migrator.py │
│  ── 共享存储层 db.py（SQLite WAL，单连接+锁）──      │
└──────────────┬─────────────────────────────────────┘
┌─ 抽象适配层（集群预留）─────────────────────────────┐
│  WorkerInterface：executor 仅上报事件，controller 聚合  │
└──────────────┬─────────────────────────────────────┘
┌─ PTY 内核层（永久冻结）─────────────────────────────┐
│  pty_host.py（零修改） / session_manager.py（仅事件钩子） │
└───────────────────────────────────────────────────┘
```

### 存储层 db.py（共享）

- 单一 `data/webpty.db`（SQLite，`sqlite3` 标准库，`PRAGMA journal_mode=WAL`）
- 单连接 + asyncio 锁（单线程事件循环内无竞争；`asyncio.Lock` 包住写事务）
- 表：
  - `notifications(id, ts, event_type, level, tool, project, session_id, title, body, dedup_key, delivered)`
  - `notification_rules(id, name, event_type, matcher_json, action, level, quiet_start, quiet_end, enabled)`
  - `token_usage(id, ts, project, tool, model, session_id, tokens_in, tokens_out, cost, source)`
  - `backups(id, filename, created_at, size_bytes, sha256, manifest_json, encrypted, retained)`
  - `migrations(id, filename, created_at, source_node, mode, status, log)`
- 聚合查询走 SQL（按项目/Agent/模型/会话 GROUP BY），满足宪章"统计数据实时聚合"

## 4. 子项目 1：任务状态通知系统

**数据流**：session_manager 事件 → 事件总线 → notifier 判定规则 → SQLite 记录 + SMTP + WebUI 推送

### 组件

| 组件 | 职责 |
|---|---|
| `notifier.py` | 订阅 `session_event` 事件；规则匹配；写 `notifications` 表；触发发送；去重 |
| `mailer.py` | SMTP 发送（smtplib 标准库），TLS/SSL 支持，HTML 模板；失败入重试队列（`notifications.delivered=0` 重试） |
| `rules.py` | 规则模型与匹配器：事件类型（completed/failed/crashed/terminated）、按 tool/项目/会话名匹配、分级（info/warn/critical）、静默时段 |
| 事件钩子 | `session_manager` 在 `_on_host_exit`/agent exit/`stop()` 处 `emit("session_event", {...})`——仅 3-4 处、零业务逻辑 |

### API

- `GET/PUT/DELETE /api/notify/rules` — 规则 CRUD
- `GET /api/notify/messages?page=` — 通知记录分页
- `POST /api/notify/test` — 测试发送

### WebUI 通知中心面板

规则列表/编辑、消息记录、历史回溯、静默时段配置。

### 去重铁律

同一 `dedup_key`（session_id+event_type+level）在 60s 窗口内合并，满足"通知不重复不遗漏"。

## 5. 子项目 2：Token & 成本管理系统

**数据流**：agent stream-json 解析（实时） + 日志扫描（事后校对） → `token_usage` 表 → SQL 聚合

### 组件

| 组件 | 职责 |
|---|---|
| `usage_parser.py` | 从 stream-json 事件提取 `{tokens_in, tokens_out, cost}`；claude 的 `usage` 字段、reasonix/opencode 统计事件；未知格式返回 None 走事后兜底 |
| `cost_tracker.py` | 订阅 `agent_event`（拦截 result 事件）→ 写 `token_usage`（source=realtime）；内存聚合缓存（当日/本周/本月）供实时面板 |
| `price_table.py` | 模型价格表：内置 claude/codex/reasonix/opencode 常见默认价（config `prices` 段，可覆盖）；按每 M token 单价折算 |
| `reconciler.py` | 事后校对：扫描 `~/.claude/projects/*.jsonl` 等日志补录缺失记录（source=posthoc） |
| `budget.py` | 预算限额：`budget.limit` 配置；超限 → 复用 notifier 告警 + 可选自动停止会话 |

### API

- `GET /api/cost/summary?period=day|week|month`
- `GET /api/cost/by-project|by-agent|by-model|by-session`
- `GET /api/cost/compare`（多模型成本对比）
- `GET /api/cost/alerts`、`PUT /api/cost/budget`

### WebUI 成本账单面板

汇总卡片、分组图表（纯前端 Canvas/SVG 绘制，无第三方图表库依赖）、成本对比表、预算设置。

## 6. 子项目 3：全自动配置备份系统

**数据流**：定时/手动 → 快照打包 → 加密（可选）→ 校验 → 记录 + 轮转

### 组件

| 组件 | 职责 |
|---|---|
| `backup.py` | 快照内容：config.json、会话注册表、通知规则、价格表、权限——统一为 `data/state/` 下可序列化状态；打包 `data/backups/webpty-<ts>.tar.gz` + `manifest.json`（版本/时间/SHA256/内容清单） |
| 加密 | 可选：检测 `cryptography`/`pycryptodome` 可用才加密（AES-GCM，密钥来自 config `backup.encryption_key` 或环境变量）；默认不加密 |
| 轮转 | 保留最近 N 份（`backup.retention` 默认 7），超限删除 |
| 定时 | asyncio 后台 task：`backup.interval_hours` 默认 24h |

### API

- `POST /api/backup/create`、`GET /api/backup/list`
- `POST /api/backup/restore/{id}`、`GET /api/backup/diff/{a}/{b}`（差异对比）

### WebUI 备份管理面板

创建/列表/恢复/差异对比。

## 7. 子项目 4：一键配置迁移 & 环境克隆

**数据流**：导出（快照）→ 传输 → 导入（恢复/克隆）→ 校验

### 组件

| 组件 | 职责 |
|---|---|
| `migrator.py` | 导出 = backup 快照 + 额外文件（价格表等）；导入 = 恢复 + 冲突合并策略 |
| 迁移包 | `webpty-migrate-<ts>.tar.gz` + manifest（schema_version、source_node_id、内容清单） |
| 环境克隆 | 模板导出（项目模板/工具配置）→ 目标导入；批量部署 = 同模板多次导入 |
| 冲突处理 | `mode`：`merge`（默认，保留现有+新增）/ `replace`（覆盖）/ `dry-run`（仅预览差异） |

### API

- `POST /api/migrate/export`、`POST /api/migrate/import`（multipart 上传）
- `POST /api/migrate/clone`、`GET /api/migrate/diff`

### WebUI 迁移向导面板

导出→下载→上传→预览差异→导入 五步向导。

### 集群预留

迁移包带 `source_node_id`；`WorkerInterface` 定义 `export_state()/import_state()` 抽象——
单机自用，集群时 controller 调各 executor 收集（接口先行，实现单机）。

## 8. 质量准则落实（对照宪章五大准则）

| 准则 | 落实 |
|---|---|
| 高效 | 统计/通知/备份全部异步任务，不阻塞主服务事件循环 |
| 快速 | SQLite 聚合查询 + 内存缓存；通知秒级触发 |
| 低占用 | 单 SQLite 文件；备份/校对任务异步轻量化；不常驻高 CPU |
| 兼容 | 全 Agent 兼容（解析器可扩展）；单机/集群双向（WorkerInterface） |
| 稳定 | 事务原子写、通知去重、备份 SHA256 校验、迁移 dry-run 预览 |

## 9. 禁止事项（对照宪章红线）

- 不实现任务队列/工作流调度（属 herdr）
- 不实现 A2A 消息/Agent 协作（属 a2abridge）
- 不实现 LLM 推理/Prompt 管理
- 不做智能决策/任务拆解
- 不修改 pty_host.py（内核冻结）；session_manager 仅加事件钩子

## 10. 测试计划

- **单元**：db.py（schema/事务/WAL）、usage_parser（各格式解析）、rules 匹配、price_table、backup 打包/校验/轮转、migrator 导入合并
- **集成**：notifier 事件→记录→(mock SMTP) 发送；cost 实时聚合+事后校对合并；backup 创建→恢复→diff；migrate 导出→导入→校验
- **现有 122 测试保持全绿**；SMTP 用 mock（不真发邮件）；备份/迁移用临时目录

## 11. 里程碑顺序（对应实施计划）

1. **M1 存储层**：db.py（SQLite WAL + 5 表 + 事务助手）
2. **M2 通知系统**：事件钩子 + notifier/rules/mailer + API + WebUI
3. **M3 成本系统**：usage_parser + cost_tracker + price_table + reconciler + API + WebUI
4. **M4 备份系统**：backup.py + 定时 + API + WebUI
5. **M5 迁移系统**：migrator.py + WorkerInterface 抽象 + API + WebUI
6. **M6 收尾**：全量测试 + 部署验证 + 文档 + 提交
