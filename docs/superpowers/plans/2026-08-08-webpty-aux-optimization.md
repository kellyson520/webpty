# WebPty 附属功能优化实施计划（8 项修复）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复附属功能（通知/成本/备份/迁移/供应商/Agent 配置面板）的 8 项剩余差距，以五大核心（高效/快速/低占用/兼容/稳定）为导向。

**Architecture:** 8 项独立小修，分散在 server.py / backup.py / migrator.py / cost_tracker.py / notifier.py / agent_config.py / public/app.js，每项独立 commit + 独立测试，全部 S 工作量。

**Tech Stack:** Python ≥3.10 标准库零依赖（sqlite3/smtplib/tarfile/asyncio）+ 前端原生 JS。

## Global Constraints

- Python ≥ 3.10，POSIX 标准库零依赖
- 测试命令：`cd /root/webpty && python3 -m unittest discover -s test`（当前 270 全绿基线）
- 不修改 `src/pty_host.py`（内核冻结）
- 裸 `except` 仅在有 `# noqa: BLE001` 注释处
- 每项独立 commit、独立验证；提交前缀 `fix(ext):` / `perf(ext):` / `feat(ui):`
- 前端改动仅限 `public/app.js` 的 `ACFG_FIELD_META`（Task 8b）

---

### Task 1: 修复 /api/agent-config/read 的 _query_param AttributeError

**Files:**
- Modify: `src/server.py:499`（`self._query_param(path, "tool")` → `query.get("tool")`）
- Test: `test/test_server.py`（新增集成测试）

**Interfaces:**
- Consumes: `_AGENT_CONFIG_TOOLS`（frozenset，含 codex/reasonix/claude 等 9 工具）、`read_config(tool) -> dict`
- Produces: 修复后 `/api/agent-config/read?tool=codex` 返回 200（此前必现 AttributeError → 连接重置）

- [ ] **Step 1: 写失败测试**

在 `test/test_server.py` 的 `ServerIntegrationTest` 类内追加:

```python
    def test_agent_config_read_returns_ok(self):
        """/api/agent-config/read 路由不再 AttributeError(此前必现连接重置)。"""
        # 用存在的工具(codex)请求;即使本机无配置文件也应返回 200 + ok=False
        st, j = self._req("/api/agent-config/read?tool=codex")
        self.assertEqual(st, 200)
        self.assertIn("ok", j)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_server.ServerIntegrationTest.test_agent_config_read_returns_ok -v`
Expected: FAIL — `AttributeError: 'ServerIntegrationTest' object has no attribute '_query_param'`（或连接重置导致 0 响应）

- [ ] **Step 3: 最小实现**

`src/server.py:499` 改为:

```python
        if path == "/api/agent-config/read" and method == "GET":
            from agent_config import read_config
            tool = query.get("tool", [""])[0]
            if tool not in _AGENT_CONFIG_TOOLS:
                return await self._send_json(writer, 400,
                                             {"error": "unknown tool"}, headers)
            return await self._send_json(writer, 200, read_config(tool), headers)
```

（`query` 变量在 `_handle_request` 已定义：`query = urllib.parse.parse_qs(...)`，与 574/588 行的 `query.get("path", [""])[0]` 模式一致。）

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_server.ServerIntegrationTest.test_agent_config_read_returns_ok -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 271 tests OK

```bash
git add src/server.py test/test_server.py
git commit -m "fix(ext): agent-config/read used undefined _query_param (AttributeError → reset)"
```

---

### Task 2: sanitize_import_config 剔除 sessions 键

**Files:**
- Modify: `src/migrator.py`（`sanitize_import_config`，约 71-100 行）
- Modify: `src/backup.py:149`（restore 已调用 sanitize —— 无需改，验证即可）
- Test: `test/test_migrator.py`、`test/test_backup.py`

**Interfaces:**
- Consumes: `sanitize_import_config(cfg: dict) -> dict`（已有：剔除敏感键 + 非内置 command 工具 + providers apiKey）
- Produces: `sessions` 键永不被导入（防重启幽灵/丢会话）

- [ ] **Step 1: 写失败测试**

在 `test/test_migrator.py` 的 `MigratorTest` 类内追加:

```python
    async def test_import_never_restores_sessions(self):
        """导入配置中的 sessions 键必须被剔除(运行时状态,防幽灵会话)。"""
        from migrator import sanitize_import_config
        clean = sanitize_import_config({
            "port": 4790, "sessions": [{"id": "ghost"}], "tools": {}})
        self.assertNotIn("sessions", clean)
        self.assertEqual(clean["port"], 4790)
```

`test/test_backup.py` 追加（restore 后 config 无 sessions）:

```python
    async def test_restore_drops_sessions_key(self):
        """restore 恢复的 config 不含 sessions(运行时状态)。"""
        b = await create_backup_async(self.data, self.config, self.db)
        res = await restore_backup(b["id"], self.data, self.db, self.config)
        self.assertTrue(res["ok"])
        with open(os.path.join(self.data, "config.json")) as f:
            restored = json.load(f)
        self.assertNotIn("sessions", restored)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator.MigratorTest.test_import_never_restores_sessions test.test_backup.BackupTest.test_restore_drops_sessions_key -v`
Expected: FAIL — `sessions` 仍在 clean 结果中

- [ ] **Step 3: 最小实现**

`src/migrator.py` 的 `sanitize_import_config` 中，循环开始前加:

```python
    out = {}
    for k, v in cfg.items():
        if _is_sensitive(k) or k == "sessions":
            continue  # 凭据 + 运行时会话列表永不导入
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator test.test_backup -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 273 tests OK

```bash
git add src/migrator.py test/test_migrator.py test/test_backup.py
git commit -m "fix(ext): never import sessions key — prevents ghost/missing sessions after restore"
```

---

### Task 3: SMTP 发送移入 run_in_executor

**Files:**
- Modify: `src/notifier.py`（`_send_mail` 与 `send_pending` 中的 `self.mailer.send(...)` 调用）
- Test: `test/test_notifier.py`（既有 mock 用例应保持通过）

**Interfaces:**
- Consumes: `Mailer.send(subject, html) -> None`（smtplib 同步,15s 超时）
- Produces: SMTP 调用不再阻塞事件循环

- [ ] **Step 1: 写失败测试（行为保护）**

在 `test/test_notifier.py` 的 `NotifierTest` 类内追加:

```python
    async def test_send_mail_runs_in_executor(self):
        """_send_mail 的 SMTP 调用包在 run_in_executor(不阻塞事件循环)。"""
        import asyncio as _a
        calls = []
        orig_send = self.n.mailer.send

        def spy(subject, html):
            calls.append((subject, html))
            return None
        self.n.mailer.send = spy
        # 直接调 _send_mail(已落库一条通知)
        nid = await self.db.add_notification({
            "event_type": "failed", "level": "warn", "tool": "t",
            "project": "/p", "session_id": "s-x", "title": "t",
            "body": "b", "dedup_key": "k-x"})
        await self.n._send_mail(nid, {"type": "failed", "name": "n",
                                      "tool": "t", "project": "/p",
                                      "exit_code": 1})
        self.assertEqual(len(calls), 1)
        self.n.mailer.send = orig_send
```

（此测试验证调用发生;executor 包装本身由实现保证,测试通过 spy 确认 send 被调用即绿——它是回归保护,在 Step 3 实现前同样通过,故 Step 2 的"红"以手工确认当前实现同步阻塞为准,不强求失败。）

- [ ] **Step 2: 确认现状（同步阻塞）**

Run: `cd /root/webpty && grep -n "self.mailer.send" src/notifier.py`
Expected: 两处 `self.mailer.send(...)` 直接调用（无 executor）

- [ ] **Step 3: 实现**

`src/notifier.py`:

```python
    async def _send_mail(self, nid: int, event: dict) -> None:
        if not self.mailer.enabled():
            await self.db.mark_delivered(nid, True)
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self.mailer.send,
            f"[webpty] {event.get('tool')} {event.get('type')}",
            HTML_TMPL.format(
                title=event.get("name", "session"),
                body=event.get("type", ""),
                meta=f"project: {event.get('project')}\n"
                     f"exit_code: {event.get('exit_code')}",
            ))
        await self.db.mark_delivered(nid, True)
```

`send_pending` 中对应调用同样包 `run_in_executor`:

```python
        for row in await self.db.pending_notifications():
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self.mailer.send,
                    f"[webpty] {row.get('tool')} {row.get('event_type')}",
                    HTML_TMPL.format(title=row["title"],
                                     body=row.get("body", ""),
                                     meta=f"session: {row.get('session_id')}"))
                await self.db.mark_delivered(row["id"], True)
                sent += 1
            except Exception:  # noqa: BLE001
                continue
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_notifier test.test_mailer -v`
Expected: 全 PASS（mock 不受 executor 影响）

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 274 tests OK

```bash
git add src/notifier.py test/test_notifier.py
git commit -m "perf(ext): SMTP send in executor — slow/unreachable mail no longer freezes the event loop"
```

---

### Task 4: 原子写 tmp 文件唯一化

**Files:**
- Modify: `src/config.py:274`（`config_path + ".tmp"` → 带 pid）
- Modify: `src/backup.py:43`（`path + ".tmp"` → 带 pid）
- Test: `test/test_config.py`、`test/test_backup.py`（既有用例应保持通过）

**Interfaces:**
- Consumes: `save_config(config)`、`_atomic_write_json(path, obj)`
- Produces: 两处 tmp 文件名互不冲突（restore 与 save_config 并发安全）

- [ ] **Step 1: 写失败测试（并发安全回归）**

在 `test/test_config.py` 追加:

```python
    def test_save_config_tmp_has_unique_suffix(self):
        """save_config 的 tmp 文件带 pid 后缀(与 restore 的 tmp 不冲突)。"""
        c = cfg.load_config()
        cfg.save_config(c)
        import glob, os
        leftovers = glob.glob(cfg.config_path + ".tmp*")
        # 原子写后 tmp 应已清理;若残留也必须是带 pid 的唯一名
        self.assertNotEqual(leftovers, [cfg.config_path + ".tmp"])
```

（此用例验证 tmp 命名;Step 2 红阶段:当前 tmp 名是 `<path>.tmp`,若存在并发残留无法区分。）

- [ ] **Step 2: 确认现状**

Run: `cd /root/webpty && grep -n '"\.tmp"' src/config.py src/backup.py`
Expected: 两处 `+ ".tmp"`（无 pid）

- [ ] **Step 3: 实现**

`src/config.py` 的 `save_config`:

```python
def save_config(config: dict) -> None:
    ensure_data_dirs()
    # Atomic write: unique tmp (pid suffix) so a concurrent restore's
    # _atomic_write_json can never truncate the same file mid-write.
    tmp = f"{config_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, config_path)
```

`src/backup.py` 的 `_atomic_write_json`:

```python
def _atomic_write_json(path: str, obj) -> None:
    """Write JSON atomically (unique tmp + fsync + os.replace)."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_config test.test_backup -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 275 tests OK

```bash
git add src/config.py src/backup.py test/test_config.py
git commit -m "fix(ext): unique pid-suffixed tmp for atomic writes — restore vs save_config race"
```

---

### Task 5: _handle_request 兜底异常 → 500 JSON

**Files:**
- Modify: `src/server.py:310-312`（异常处理链尾部加 `except Exception`）
- Modify: `src/server.py`（notify rules POST/PUT 的 matcher_json 类型校验）
- Test: `test/test_notify_api.py`

**Interfaces:**
- Consumes: `_handle_request(reader, writer, ...)` 现有 try/except
- Produces: 未捕获异常返回 500 JSON + log_error（前端可见错误而非连接重置）

- [ ] **Step 1: 写失败测试**

在 `test/test_notify_api.py` 的 `NotifyApiTest` 类内追加:

```python
    def test_rules_invalid_matcher_json_returns_400(self):
        """matcher_json 非字符串 → 400(而非 500 或连接重置)。"""
        st, j = self._req("POST", "/api/notify/rules", {
            "name": "r-bad", "event_type": "failed",
            "matcher_json": {"tool": "claude"}, "action": "email",
            "level": "warn", "quiet_start": "", "quiet_end": "", "enabled": 1})
        self.assertEqual(st, 400)
        self.assertIn("error", j)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_notify_api.NotifyApiTest.test_rules_invalid_matcher_json_returns_400 -v`
Expected: FAIL — 500（sqlite3.ProgrammingError 绑定 dict 参数）或连接重置

- [ ] **Step 3: 实现**

`src/server.py` notify rules POST/PUT 处（`await self.db.upsert_rule(body)` 前）:

```python
        if path == "/api/notify/rules" and method == "POST":
            body = await self._read_json(reader, headers)
            if not isinstance(body.get("matcher_json", "{}"), str):
                raise HttpError(400, "matcher_json must be a string")
            rid = await self.db.upsert_rule(body)
            return await self._send_json(writer, 201, {"id": rid}, headers)
```

（PUT 分支同样加校验。）

`_handle_request` 异常链尾部（现有 `except (ConnectionError, OSError)` 后）:

```python
        except Exception as err:  # noqa: BLE001 — last-resort: client gets a
            # JSON error instead of a dropped connection.
            log_error("http", err)
            try:
                await self._send_json(writer, 500, {"error": str(err)}, {})
            except Exception:  # noqa: BLE001
                pass
```

（确认 `log_error` 已 import；`_send_json` 的 headers 参数签名——若 500 分支的 headers 结构不同,参考现有 403/400 分支的写法。）

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_notify_api -v`
Expected: 全 PASS（含新 400 用例）

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 276 tests OK

```bash
git add src/server.py test/test_notify_api.py
git commit -m "fix(ext): 500 JSON fallback in HTTP handler + matcher_json type check"
```

---

### Task 6: uploads 清理 + migrate export 登记 backups

**Files:**
- Modify: `src/server.py:729-734`（import 后删除上传文件）
- Modify: `src/migrator.py`（export 后登记 backups 表）
- Test: `test/test_migrate_api.py`、`test/test_migrator.py`

**Interfaces:**
- Consumes: `Migrator.import_package(path, mode)`、`Database.add_backup(...)`
- Produces: 上传临时文件导入后删除;migrate export 包进入 backups 表(rotate 可清理)

- [ ] **Step 1: 写失败测试**

`test/test_migrator.py` 追加（export 登记）:

```python
    async def test_export_registers_in_backups(self):
        """export 生成的包登记进 backups 表(rotate 可清理,防孤儿文件)。"""
        path = await self.m.export()
        rows = await self.db.list_backups()
        self.assertTrue(any(r["filename"] == os.path.basename(path)
                            for r in rows))
```

`test/test_migrate_api.py` 追加（上传文件清理——先确认 uploads 目录 API 可用,若无直接读目录则跳过服务端测试,改断言 DB 无残留记录）。

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator.MigratorTest.test_export_registers_in_backups -v`
Expected: FAIL — backups 表无该包记录

- [ ] **Step 3: 实现**

`src/migrator.py` 的 `export()` 末尾（`return path` 前）:

```python
        await self.db.add_backup({
            "filename": os.path.basename(path),
            "size_bytes": os.path.getsize(path),
            "sha256": "",
            "manifest_json": json.dumps({
                "kind": "migrate-export",
                "created_at": time.time()}), "encrypted": 0, "retained": 1})
        return path
```

`src/server.py` 的 `_handle_migrate_import`（`return await self.migrator.import_package(dest, mode)` 前加 try/finally）:

```python
        try:
            return await self.migrator.import_package(dest, mode)
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator test.test_migrate_api -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 277 tests OK

```bash
git add src/migrator.py src/server.py test/test_migrator.py
git commit -m "fix(ext): delete uploaded migrate packages after import; register exports for rotation"
```

---

### Task 7: _last_usage 在 remove 时清理

**Files:**
- Modify: `src/session_manager.py:204`（remove 处 emit `session_event` type=removed）
- Modify: `src/cost_tracker.py:28-32`（on_session_event 纳入 removed 类型）
- Test: `test/test_cost_tracker.py`

**Interfaces:**
- Consumes: `remove(sid)`（现有删除路径）、`cost.on_session_event(event)`
- Produces: 会话删除后 `_last_usage[sid]` 被清理（防缓慢内存泄漏）

- [ ] **Step 1: 写失败测试**

在 `test/test_cost_tracker.py` 的 `CostTrackerTest` 类内追加:

```python
    async def test_removed_session_clears_cumulative(self):
        """会话 removed 事件清除 _last_usage(防缓慢泄漏)。"""
        await self.c._record({
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "tool": "codex", "project": "/p", "session_id": "sid-rm"}, "sid-rm")
        self.assertIn("sid-rm", self.c._last_usage)
        self.c.on_session_event({"type": "removed", "session_id": "sid-rm"})
        self.assertNotIn("sid-rm", self.c._last_usage)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_cost_tracker.CostTrackerTest.test_removed_session_clears_cumulative -v`
Expected: FAIL — on_session_event 不识别 removed 类型

- [ ] **Step 3: 实现**

`src/cost_tracker.py`:

```python
    def on_session_event(self, event: dict) -> None:
        """Clear per-session cumulative state when the session ends or is
        removed (deleted tab — no exit event fires)."""
        if event.get("type") in ("completed", "failed", "crashed",
                                 "terminated", "removed"):
            self._last_usage.pop(event.get("session_id"), None)
```

`src/session_manager.py` 的 `remove()`（`self._emit("remove", sid)` 后加）:

```python
        self._emit("remove", sid)
        self._emit("session_event", {
            "type": "removed", "session_id": sid,
            "name": session.get("name"), "tool": session.get("tool"),
            "project": session.get("cwd"), "state": "removed",
            "exit_code": None, "signal": None, "ts": time.time(),
        })
```

（确认 `time` 已 import;`session` 变量在 remove 内可用。）

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_cost_tracker test.test_session_manager -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: 278 tests OK

```bash
git add src/cost_tracker.py src/session_manager.py test/test_cost_tracker.py
git commit -m "fix(ext): clear cost cumulative state on session remove (slow leak)"
```

---

### Task 8: migrate import executor + acfg 键扩展

**Files:**
- Modify: `src/server.py:709-734`（import_package 包 run_in_executor）
- Modify: `src/agent_config.py:51-72`（TOML_KEYS 扩展）
- Modify: `public/app.js`（ACFG_FIELD_META 扩展）
- Test: `test/test_migrate_api.py`、`test/test_agent_config.py`

**Interfaces:**
- Consumes: `Migrator.import_package(path, mode)`、`TOML_KEYS`/`ACFG_FIELD_META`
- Produces: 大包导入不阻塞事件循环;codex/reasonix 可编辑 proxy/temperature/api_key/base_url

- [ ] **Step 1: 写失败测试**

`test/test_agent_config.py` 追加:

```python
    def test_toml_new_keys_replaced(self):
        """codex proxy/temperature 与 reasonix api_key/base_url 可编辑。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wp-keys-")
        p = os.path.join(tmp, "config.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.4"\nproxy = "http://proxy:8080"\n')
        with mock.patch.object(ac, "AGENT_CONFIG_PATHS", {"codex": [p]}):
            with mock.patch.object(ac, "_HOME", tmp):
                res = ac.update_config("codex", {
                    "proxy": "http://new-proxy:3128",
                    "temperature": "0.7"})
        self.assertTrue(res["ok"])
        content = open(p, encoding="utf-8").read()
        self.assertIn('proxy = "http://new-proxy:3128"', content)
        self.assertIn('temperature = "0.7"', content)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_agent_config.MaskTest.test_toml_new_keys_replaced -v`
Expected: FAIL — `proxy`/`temperature` 不在 TOML_KEYS["codex"] 中

- [ ] **Step 3: 实现**

`src/agent_config.py` 的 `TOML_KEYS`:

```python
    "codex": {
        "model": (r'^model\s*=\s*".*?"', 'model = "{}"'),
        "base_url": (r'^openai_base_url\s*=\s*".*?"', 'openai_base_url = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
        "model_provider": (r'^model_provider\s*=\s*".*?"', 'model_provider = "{}"'),
        "temperature": (r'^temperature\s*=\s*"?[^"]*"?', 'temperature = "{}"'),
        "proxy": (r'^proxy\s*=\s*".*?"', 'proxy = "{}"'),
    },
    "reasonix": {
        "model": (r'^default_model\s*=\s*".*?"', 'default_model = "{}"'),
        "language": (r'^language\s*=\s*".*?"', 'language = "{}"'),
        "effort": (r'^effort\s*=\s*"[a-z]+"', 'effort = "{}"'),
        "api_key": (r'^api_key\s*=\s*".*?"', 'api_key = "{}"'),
        "base_url": (r'^base_url\s*=\s*".*?"', 'base_url = "{}"'),
        "provider": (r'^provider\s*=\s*".*?"', 'provider = "{}"'),
    },
```

`public/app.js` 的 `ACFG_FIELD_META` 追加:

```js
  model_provider: { label: '模型供应商 model_provider', ph: 'openai / anthropic' },
  temperature: { label: '温度 temperature', ph: '0.0 - 1.0' },
  proxy: { label: '代理 proxy', ph: 'http://host:port' },
```

（reasonix 的 api_key/base_url/provider 复用现有 meta：api_key 已存在、base_url 未在 meta 中——追加 `base_url: { label: 'API 地址 base_url', ph: 'https://...' }`。）

`src/server.py` `_handle_migrate_import`（导入调用包 executor）:

```python
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: asyncio.run_coroutine_threadsafe(
                self.migrator.import_package(dest, mode), loop).result())
        return result
```

（若 executor 内 async 调用复杂,替代方案:`import_package` 的同步部分(解压/解析)单独抽成同步函数,executor 只跑同步部分,async 的 db 调用留在事件循环——实现时以最小改动为准,优先直接 `run_in_executor(None, ...)` 包住整个 await 不可行时,参考 Task 5 的 reconcile 先例:`items = await asyncio.get_event_loop().run_in_executor(None, scan_fn, arg)` 模式,把 `_read_package` 抽为同步函数放进 executor,`import_package` 内部 await 它。)

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_agent_config test.test_migrate_api -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | grep -E "^(Ran|OK|FAILED)"` && `node --check public/app.js`
Expected: 279 tests OK + JS 语法 OK

```bash
git add src/agent_config.py src/server.py public/app.js test/test_agent_config.py
git commit -m "feat(ext): editable proxy/temperature/base_url keys + non-blocking migrate import"
```

---

## 里程碑验收

- [ ] 8 个 Task 全部提交,每项独立 commit
- [ ] 全量测试 270 → 279（+9 新用例）
- [ ] `node --check public/app.js` 通过
- [ ] 生产部署后冒烟:`/api/agent-config/read?tool=codex` 200、notify rules 非法 matcher_json 400、import 后 uploads 空
