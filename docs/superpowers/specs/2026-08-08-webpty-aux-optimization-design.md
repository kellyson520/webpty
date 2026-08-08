# WebPty 附属功能优化设计（五大核心导向，8 项修复）

> 状态：设计中（brainstorming → 已获用户批准）
> 日期：2026-08-08
> 目标：以五大核心（高效/快速/低占用/兼容/稳定）为导向，修复附属功能（通知/成本/备份/迁移/供应商/Agent 配置面板）的剩余差距。

## 背景

accuracy-speed-core 计划（10 任务）与 3 个安全 issue 已完成，核心链路已充分优化。深度审计 v2 聚焦**附属功能**，产出 8 项高价值修复（全部 S 工作量，低风险，每项独立提交）。用户已批准全部 8 项。

## 设计（8 项）

### 1. 【稳定·高】修复 Acfg 面板必现 bug — server.py:499

**现状**：`/api/agent-config/read` 调用不存在的 `self._query_param(path, "tool")` → AttributeError → 连接重置，前端静默吞掉 → **Agent 配置面板读取 100% 失败**（测试未覆盖此路由）。

**方案**：改为 `query.get("tool")`（与 server.py:574/588 先例一致）。补 server 路由层测试（test_agent_config 现有只测模块层）。

### 2. 【兼容·中高】restore/import 排除 sessions 键 — backup.py:149-153 / migrator.py:203-208

**现状**：`collect_state` 打包的 config 含 `sessions`（备份时刻列表）；restore 的 `merged.update(cfg)` 与 import 的 `self.config.update(incoming)` 带入该键 → 重启后 inflate 出备份/导出方的会话（幽灵）或丢失本机新会话。注释说"sessions 是运行时状态,不恢复"但实现未排除。

**方案**：`sanitize_import_config` 剔除 `sessions` 键（一处修复两路径）。补测试：restore 后 config 无 sessions、import 后无 sessions。

### 3. 【快速·中高】SMTP 异步化 — notifier.py:70-82,98-106 / mailer.py:31-48

**现状**：`smtplib` 15s 超时全程同步，`send_pending`（5min）/`test_message`/事件邮件在事件循环内直接调用 → SMTP 慢/不通时 UI 冻结 ≤15s/封。

**方案**：notifier 的 `_send_mail` 与 `send_pending` 中 `self.mailer.send(...)` 调用包 `asyncio.get_running_loop().run_in_executor(None, ...)`。SMTP 连通性测试不受影响（mock 已存在）。

### 4. 【稳定·中】原子写 tmp 文件唯一化 — backup.py:41-49 / config.py:274

**现状**：restore 的 `_atomic_write_json` 与 `save_config` 都用 `<config_path>.tmp`，并发时互相截断 → config.json 损坏。

**方案**：统一 tmp 名带 pid 后缀（`<path>.tmp.<pid>`）——`os.replace` 本身原子，tmp 唯一即安全。backup 与 config 两处同步改。

### 5. 【稳定·中】`_handle_request` 兜底 500 — server.py:310-312

**现状**：仅捕获 HttpError/ConnectionError/OSError；未捕获路径（rules matcher_json 非字符串 → sqlite3.ProgrammingError；restore/diff 损坏 tar → TarError/None.read()）→ 连接重置，前端无提示。

**方案**：`except Exception` 兜底 → 500 JSON `{"error": str(err)}` + log_error；同时给 notify rules 输入加类型校验（matcher_json 非 str → 400）。

### 6. 【低占用·中】uploads 残留 + migrate export 孤儿 — server.py:729-734 / migrator.py:147-154

**现状**：import 上传文件（≤50MB）永不删除；export 包写 `backups/` 但不进 DB，rotate 不清理。

**方案**：import 后 `finally: os.remove(dest)`；export 的包在完成后由调用方清理或登记进 backups 表（选后者——`add_backup` 一条记录，rotate 自然清理）。

### 7. 【稳定·中】`_last_usage` remove 时清理 — cost_tracker.py:28-32 / session_manager.py:178-205

**现状**：`on_session_event` 只清 completed/failed/crashed/terminated；`remove()`（删标签页）不 emit `session_event` → 每个被删会话的 4 值 dict 常驻（缓慢泄漏）。

**方案**：`remove()` 时 emit `session_event`(type=removed)（notifier 忽略该类型，cost_tracker 的 on_session_event 将其纳入清理类型列表）。补测试：remove 后 `_last_usage` 无该 sid。

### 8. 【快速/兼容·S】import executor + acfg 键扩展 — server.py:709-734 / agent_config.py:51-72

**8a**：`import_package` 整体包进 `run_in_executor`（50MB 包解压数百 ms 不阻塞事件循环）；`parse_multipart` 改为只定位边界索引避免全量 split 拷贝。
**8b**：TOML_KEYS 补 codex `model_provider`/`temperature`/`proxy`、reasonix `api_key`/`base_url`/`provider`；前端 ACFG_FIELD_META 同步（proxy 显示为普通字段，api_key 为密码框）。

## 测试计划

每项补 1-2 个用例：
- #1：test_server 或新 test_agent_config_api——GET read 返回 200
- #2：test_backup_api 恢复后 config 无 sessions；test_migrate_api import 后无 sessions
- #3：test_notifier 的 SMTP mock 仍通过（executor 包装不改变结果）
- #4：并发写测试（两处 tmp 名不同）
- #5：test_notify_api rules 非法 matcher_json → 400；损坏包 restore → 500 JSON
- #6：import 后 uploads 目录无文件；export 登记 backups
- #7：test_cost_tracker remove 后 _last_usage 清理
- #8a：migrate import 大包不阻塞（mock executor）；#8b：acfg 新键替换测试

## 约束

- Python ≥ 3.10 标准库零依赖
- 每项独立 commit、独立验证（`python3 -m unittest discover -s test` 全绿）
- 不修改 src/pty_host.py（内核冻结）
- 前端改动仅 acfg 键扩展（app.js 的 ACFG_FIELD_META）
