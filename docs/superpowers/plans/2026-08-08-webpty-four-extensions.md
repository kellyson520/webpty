# WebPty 四大合规扩展实施计划（通知 / 成本 / 备份 / 迁移）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 webpty 落地宪章四大扩展：任务状态通知、Token&成本管理、全自动配置备份、一键配置迁移，全部跑在现有 Python 标准库零依赖架构上。

**Architecture:** 所有新能力落在业务管理层（新增 `src/db.py` 共享存储 + `src/notifier.py`/`src/rules.py`/`src/mailer.py`/`src/usage_parser.py`/`src/price_table.py`/`src/cost_tracker.py`/`src/reconciler.py`/`src/backup.py`/`src/migrator.py` 模块），通过 session_manager 事件总线（`sessions.on(...)`）接入；PTY 内核层（`pty_host.py`）零修改，`session_manager.py` 仅加事件 emit。REST 端点照抄 `_route` 的 if-链 + `re.match` 模式。WebUI 复用 `hidden` 弹层模式（backdrop+panel）。

**Tech Stack:** Python ≥3.10 标准库（sqlite3 / smtplib / tarfile / hashlib / json / asyncio），零第三方依赖；测试用 `unittest` + `unittest.mock`（SMTP 全 mock，不真发邮件）。

## Global Constraints

- Python ≥ 3.10，POSIX 标准库零依赖（备份加密为可选增强：检测到 `cryptography` 才启用 AES-GCM，默认不加密）
- 测试命令：`python3 -m unittest discover -s test`（现有 134 个测试必须保持全绿）
- 现有代码风格：PEP8、类型注解；裸 `except` 仅在有 `# noqa: BLE001` 注释处
- 禁止修改 `src/pty_host.py`（内核冻结）；`session_manager.py` 仅新增事件 emit（不承载业务逻辑）
- 数据库文件：`os.path.join(data_dir, "webpty.db")`，`data_dir` 来自 `src/config.py` 的模块级变量（`WEBPTY_DATA_DIR` env 或 `~/.config/webpty`）
- 所有新增 API 走 `/api/notify/*`、`/api/cost/*`、`/api/backup/*`、`/api/migrate/*` 前缀（`_route` 的 if-链，注意放在 `/api/sessions` 正则之后、静态兜底之前）
- 前端新面板复用 `hidden` 弹层模式（`#menu-backdrop` 同款 backdrop+panel 容器），无第三方库
- 每任务独立可验证、独立 commit；提交消息 `feat(ext): ...` / `test(ext): ...` 前缀
- 集成测试复制 `test/test_server.py` 的起服样板（env 注入 `WEBPTY_DATA_DIR` + `_pick_port()` + 轮询就绪）
- 审计安全：导入/恢复路径必须防目录穿越（`os.path.realpath` + 前缀校验）；multipart 上传仅支持单文件字段 `file`，大小上限 50MB

---

### Task 1: 共享存储层 db.py（SQLite WAL + 5 表 + 事务助手）

**Files:**
- Create: `src/db.py`
- Test: `test/test_db.py`

**Interfaces:**
- Consumes: `src/config.py` 的 `data_dir`（模块级变量，直接 `from config import data_dir`）
- Produces（后续所有任务依赖）:
  - `class Database:` — `__init__(self, path: str)`; `connect(self) -> None`（建表）; `async execute(self, sql, params=()) -> int`（asyncio.Lock 保护，返回 lastrowid）; `async query(self, sql, params=()) -> list[dict]`; `async query_one(self, sql, params=()) -> dict | None`; `close(self) -> None`
  - 通知表方法: `async add_notification(self, n: dict) -> int`; `async list_notifications(self, page: int, page_size: int = 20) -> dict`（返回 `{"total": int, "items": [dict]}`）; `async dedup_recent(self, dedup_key: str, window_s: float = 60.0) -> bool`（窗口内有同 key 返回 True）; `async mark_delivered(self, notif_id: int, delivered: bool) -> None`; `async pending_notifications(self, limit: int = 50) -> list[dict]`（delivered=0）
  - 规则表方法: `async list_rules(self) -> list[dict]`; `async upsert_rule(self, rule: dict) -> int`; `async delete_rule(self, rule_id: int) -> None`
  - 用量表方法: `async add_usage(self, u: dict) -> int`; `async usage_summary(self, period: str) -> dict`; `async usage_grouped(self, group: str, period: str) -> list[dict]`
  - 备份表方法: `async add_backup(self, b: dict) -> int`; `async list_backups(self) -> list[dict]`; `async get_backup(self, backup_id: int) -> dict | None`; `async delete_backup(self, backup_id: int) -> None`
  - 迁移表方法: `async add_migration(self, m: dict) -> int`; `async list_migrations(self) -> list[dict]`

- [ ] **Step 1: 写失败测试**

`test/test_db.py`（完整文件）:

```python
import asyncio
import os
import tempfile
import unittest

from db import Database


class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-db-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()

    def tearDown(self):
        self.db.close()

    async def test_schema_creates_all_tables(self):
        rows = await self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = [r["name"] for r in rows]
        for t in ("notifications", "notification_rules", "token_usage",
                  "backups", "migrations"):
            self.assertIn(t, names)

    async def test_wal_mode_enabled(self):
        row = await self.db.query_one("PRAGMA journal_mode")
        self.assertEqual(row["journal_mode"], "wal")

    async def test_notification_crud_and_dedup(self):
        nid = await self.db.add_notification({
            "event_type": "completed", "level": "info", "tool": "claude",
            "project": "/p", "session_id": "s1", "title": "t",
            "body": "b", "dedup_key": "s1|completed|info"})
        self.assertGreater(nid, 0)
        self.assertTrue(await self.db.dedup_recent("s1|completed|info", 60))
        self.assertFalse(await self.db.dedup_recent("s1|completed|warn", 60))
        self.assertFalse(await self.db.dedup_recent("s1|completed|info", 0))
        page = await self.db.list_notifications(1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["session_id"], "s1")
        await self.db.mark_delivered(nid, True)
        self.assertEqual(len(await self.db.pending_notifications()), 0)

    async def test_rules_upsert_delete(self):
        rid = await self.db.upsert_rule({
            "name": "r1", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "critical",
            "quiet_start": "22:00", "quiet_end": "08:00", "enabled": 1})
        rules = await self.db.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "r1")
        await self.db.upsert_rule({
            "id": rid, "name": "r1b", "event_type": "failed",
            "matcher_json": "{}", "action": "email", "level": "critical",
            "quiet_start": "", "quiet_end": "", "enabled": 1})
        self.assertEqual(len(await self.db.list_rules()), 1)
        await self.db.delete_rule(rid)
        self.assertEqual(len(await self.db.list_rules()), 0)

    async def test_usage_summary_and_grouped(self):
        for i in range(3):
            await self.db.add_usage({
                "project": f"/p{i}", "tool": "claude", "model": "claude-4",
                "session_id": f"s{i}", "tokens_in": 1000, "tokens_out": 500,
                "cost": 0.05, "source": "realtime"})
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 3000)
        self.assertEqual(s["tokens_out"], 1500)
        self.assertAlmostEqual(s["cost"], 0.15, places=6)
        grouped = await self.db.usage_grouped("project", "day")
        self.assertEqual(len(grouped), 3)
        self.assertAlmostEqual(sum(g["cost"] for g in grouped), 0.15, places=6)

    async def test_backup_and_migration_tables(self):
        bid = await self.db.add_backup({
            "filename": "webpty-1.tar.gz", "size_bytes": 10, "sha256": "x",
            "manifest_json": "{}", "encrypted": 0, "retained": 1})
        self.assertIsNotNone(await self.db.get_backup(bid))
        self.assertEqual(len(await self.db.list_backups()), 1)
        await self.db.delete_backup(bid)
        self.assertIsNone(await self.db.get_backup(bid))
        mid = await self.db.add_migration({
            "filename": "webpty-migrate-1.tar.gz", "source_node": "node-a",
            "mode": "merge", "status": "done", "log": "ok"})
        self.assertEqual(len(await self.db.list_migrations()), 1)
        self.assertGreater(mid, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_db -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: 最小实现**

`src/db.py`（完整文件）:

```python
"""Shared SQLite storage for the compliance extensions (notifications, cost,
backups, migrations). Standard library only, WAL mode, single connection
guarded by an asyncio.Lock (single event loop ⇒ no cross-thread contention).

Layer rule: this is business-management-layer storage; the PTY kernel never
imports it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time


def _ts() -> float:
    return time.time()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _create_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                tool TEXT,
                project TEXT,
                session_id TEXT,
                title TEXT NOT NULL,
                body TEXT,
                dedup_key TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_notif_dedup
                ON notifications (dedup_key, ts);
            CREATE INDEX IF NOT EXISTS idx_notif_ts
                ON notifications (ts);

            CREATE TABLE IF NOT EXISTS notification_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                matcher_json TEXT NOT NULL DEFAULT '{}',
                action TEXT NOT NULL DEFAULT 'email',
                level TEXT NOT NULL DEFAULT 'info',
                quiet_start TEXT NOT NULL DEFAULT '',
                quiet_end TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                project TEXT,
                tool TEXT,
                model TEXT,
                session_id TEXT,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'realtime'
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON token_usage (ts);

            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at REAL NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                manifest_json TEXT NOT NULL DEFAULT '{}',
                encrypted INTEGER NOT NULL DEFAULT 0,
                retained INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at REAL NOT NULL,
                source_node TEXT,
                mode TEXT NOT NULL DEFAULT 'merge',
                status TEXT NOT NULL DEFAULT 'pending',
                log TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        assert self._conn is not None
        async with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._conn is not None
        async with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    # ---- notifications -------------------------------------------------
    async def add_notification(self, n: dict) -> int:
        return await self.execute(
            """INSERT INTO notifications
               (ts, event_type, level, tool, project, session_id, title, body,
                dedup_key, delivered)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (n.get("ts", _ts()), n["event_type"], n.get("level", "info"),
             n.get("tool"), n.get("project"), n.get("session_id"),
             n["title"], n.get("body"), n["dedup_key"]))

    async def list_notifications(self, page: int, page_size: int = 20) -> dict:
        total = await self.query_one(
            "SELECT COUNT(*) AS c FROM notifications")
        rows = await self.query(
            "SELECT * FROM notifications ORDER BY ts DESC LIMIT ? OFFSET ?",
            (page_size, (max(page, 1) - 1) * page_size))
        return {"total": total["c"] if total else 0, "items": rows}

    async def dedup_recent(self, dedup_key: str, window_s: float = 60.0) -> bool:
        if window_s <= 0:
            return False
        row = await self.query_one(
            "SELECT 1 AS x FROM notifications WHERE dedup_key=? AND ts>=? LIMIT 1",
            (dedup_key, _ts() - window_s))
        return bool(row)

    async def mark_delivered(self, notif_id: int, delivered: bool) -> None:
        await self.execute(
            "UPDATE notifications SET delivered=? WHERE id=?",
            (1 if delivered else 0, notif_id))

    async def pending_notifications(self, limit: int = 50) -> list[dict]:
        return await self.query(
            "SELECT * FROM notifications WHERE delivered=0 ORDER BY ts ASC LIMIT ?",
            (limit,))

    # ---- rules ---------------------------------------------------------
    async def list_rules(self) -> list[dict]:
        return await self.query(
            "SELECT * FROM notification_rules ORDER BY id")

    async def upsert_rule(self, rule: dict) -> int:
        rid = rule.get("id")
        if rid:
            await self.execute(
                """UPDATE notification_rules SET name=?, event_type=?,
                   matcher_json=?, action=?, level=?, quiet_start=?,
                   quiet_end=?, enabled=? WHERE id=?""",
                (rule["name"], rule["event_type"], rule.get("matcher_json", "{}"),
                 rule.get("action", "email"), rule.get("level", "info"),
                 rule.get("quiet_start", ""), rule.get("quiet_end", ""),
                 1 if rule.get("enabled", 1) else 0, rid))
            return rid
        return await self.execute(
            """INSERT INTO notification_rules
               (name, event_type, matcher_json, action, level, quiet_start,
                quiet_end, enabled) VALUES (?,?,?,?,?,?,?,?)""",
            (rule["name"], rule["event_type"], rule.get("matcher_json", "{}"),
             rule.get("action", "email"), rule.get("level", "info"),
             rule.get("quiet_start", ""), rule.get("quiet_end", ""),
             1 if rule.get("enabled", 1) else 0))

    async def delete_rule(self, rule_id: int) -> None:
        await self.execute("DELETE FROM notification_rules WHERE id=?",
                           (rule_id,))

    # ---- token_usage ---------------------------------------------------
    async def add_usage(self, u: dict) -> int:
        return await self.execute(
            """INSERT INTO token_usage
               (ts, project, tool, model, session_id, tokens_in, tokens_out,
                cost, source) VALUES (?,?,?,?,?,?,?,?,?)""",
            (u.get("ts", _ts()), u.get("project"), u.get("tool"),
             u.get("model"), u.get("session_id"),
             int(u.get("tokens_in", 0)), int(u.get("tokens_out", 0)),
             float(u.get("cost", 0.0)), u.get("source", "realtime")))

    def _period_start(self, period: str) -> float:
        now = time.time()
        if period == "week":
            return now - 7 * 86400
        if period == "month":
            return now - 30 * 86400
        return now - 86400  # day (default)

    async def usage_summary(self, period: str) -> dict:
        row = await self.query_one(
            """SELECT COALESCE(SUM(tokens_in),0) AS tokens_in,
                      COALESCE(SUM(tokens_out),0) AS tokens_out,
                      COALESCE(SUM(cost),0) AS cost,
                      COUNT(*) AS entries
               FROM token_usage WHERE ts>=?""",
            (self._period_start(period),))
        return dict(row) if row else {"tokens_in": 0, "tokens_out": 0,
                                      "cost": 0.0, "entries": 0}

    async def usage_grouped(self, group: str, period: str) -> list[dict]:
        col = {"project": "project", "tool": "tool", "model": "model",
               "session_id": "session_id"}.get(group, "project")
        return await self.query(
            f"""SELECT {col} AS name,
                       COALESCE(SUM(tokens_in),0) AS tokens_in,
                       COALESCE(SUM(tokens_out),0) AS tokens_out,
                       COALESCE(SUM(cost),0) AS cost
                FROM token_usage WHERE ts>=? GROUP BY {col}
                ORDER BY cost DESC""",
            (self._period_start(period),))

    # ---- backups -------------------------------------------------------
    async def add_backup(self, b: dict) -> int:
        return await self.execute(
            """INSERT INTO backups
               (filename, created_at, size_bytes, sha256, manifest_json,
                encrypted, retained) VALUES (?,?,?,?,?,?,?)""",
            (b["filename"], b.get("created_at", _ts()),
             int(b.get("size_bytes", 0)), b.get("sha256", ""),
             b.get("manifest_json", "{}"),
             1 if b.get("encrypted") else 0,
             1 if b.get("retained", 1) else 0))

    async def list_backups(self) -> list[dict]:
        return await self.query("SELECT * FROM backups ORDER BY created_at DESC")

    async def get_backup(self, backup_id: int) -> dict | None:
        return await self.query_one("SELECT * FROM backups WHERE id=?",
                                    (backup_id,))

    async def delete_backup(self, backup_id: int) -> None:
        await self.execute("DELETE FROM backups WHERE id=?", (backup_id,))

    # ---- migrations ----------------------------------------------------
    async def add_migration(self, m: dict) -> int:
        return await self.execute(
            """INSERT INTO migrations
               (filename, created_at, source_node, mode, status, log)
               VALUES (?,?,?,?,?,?)""",
            (m["filename"], m.get("created_at", _ts()), m.get("source_node"),
             m.get("mode", "merge"), m.get("status", "pending"),
             m.get("log", "")))

    async def list_migrations(self) -> list[dict]:
        return await self.query("SELECT * FROM migrations ORDER BY created_at DESC")


def serialize_manifest(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_db -v`
Expected: 7 tests PASS（含 WAL 模式断言）

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | tail -3`
Expected: 现有 134 + 7 = 141 tests OK

```bash
git add src/db.py test/test_db.py
git commit -m "feat(ext): shared SQLite storage layer (WAL, 5 tables, asyncio-locked)"
```

---

### Task 2: 事件钩子 + 通知规则匹配 rules.py

**Files:**
- Create: `src/rules.py`
- Modify: `src/session_manager.py`（仅 3 处事件 emit，零业务逻辑）
- Test: `test/test_rules.py`

**Interfaces:**
- Consumes: session dict 字段 `id/name/cwd/tool/state/exit_code/signal/engine`
- Produces:
  - `src/rules.py`:
    - `EVENT_TYPES = ("completed", "failed", "crashed", "terminated")`
    - `def match_rule(rule: dict, event: dict) -> bool` — rule 的 `event_type` 与 event["type"] 相等；rule 的 `matcher_json`（JSON 字符串，形如 `{"tool": "claude", "project": "/x"}`）子集匹配 event 的 tool/project/session_id/name；matcher 为空 `{}` 即匹配全部该类型
    - `def quiet_hours_active(rule: dict, now=None) -> bool` — `quiet_start`/`quiet_end` 形如 "22:00"/"08:00"；空串不限制；跨午夜（start>end）正确处理
  - session_manager 新增事件 `session_event`（payload: `{"type", "session_id", "name", "tool", "project"(=cwd), "state", "exit_code", "signal", "ts"}`）
- 事件点（仅 3 处 emit，不 import 任何扩展模块）:
  - `_on_host_exit`（现 L608-622）：pty 会话退出 → emit `session_event`，type 由 exit_code/signal 决定：exit_code==0 → `completed`；exit_code>0 → `failed`；signal → `crashed`
  - `wait_exit`（agent 会话，现 L380-398）：agent 退出 → 同上逻辑 emit
  - `stop()`（现 L508-538）：显式停止 → emit `terminated`

- [ ] **Step 1: 写失败测试**

`test/test_rules.py`:

```python
import unittest
from datetime import datetime

from rules import match_rule, quiet_hours_active


def ev(**kw):
    base = {"type": "completed", "session_id": "s1", "name": "n1",
            "tool": "claude", "project": "/p", "state": "stopped",
            "exit_code": 0, "signal": None}
    base.update(kw)
    return base


class MatchRuleTest(unittest.TestCase):
    def test_event_type_required(self):
        rule = {"event_type": "failed", "matcher_json": "{}"}
        self.assertTrue(match_rule(rule, ev(type="failed")))
        self.assertFalse(match_rule(rule, ev(type="completed")))

    def test_matcher_subset(self):
        rule = {"event_type": "completed",
                "matcher_json": '{"tool": "claude", "project": "/p"}'}
        self.assertTrue(match_rule(rule, ev()))
        self.assertFalse(match_rule(rule, ev(tool="codex")))
        self.assertFalse(match_rule(rule, ev(project="/other")))

    def test_empty_matcher_matches_all(self):
        rule = {"event_type": "completed", "matcher_json": "{}"}
        self.assertTrue(match_rule(rule, ev(tool="anything", project="/x")))

    def test_bad_matcher_json_is_no_match(self):
        rule = {"event_type": "completed", "matcher_json": "{bad"}
        self.assertFalse(match_rule(rule, ev()))


class QuietHoursTest(unittest.TestCase):
    def test_empty_means_no_quiet(self):
        rule = {"quiet_start": "", "quiet_end": ""}
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 12, 0)))

    def test_within_window(self):
        rule = {"quiet_start": "22:00", "quiet_end": "08:00"}
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 23, 30)))
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 3, 0)))
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 12, 0)))

    def test_non_wrapping_window(self):
        rule = {"quiet_start": "09:00", "quiet_end": "17:00"}
        self.assertTrue(quiet_hours_active(rule, datetime(2026, 8, 8, 10, 0)))
        self.assertFalse(quiet_hours_active(rule, datetime(2026, 8, 8, 20, 0)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_rules -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rules'`

- [ ] **Step 3: 实现 rules.py + session_manager 事件钩子**

`src/rules.py`（完整）:

```python
"""Notification rule model and matcher (business-management layer).

Rules match session_event payloads; quiet hours suppress delivery windows.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

EVENT_TYPES = ("completed", "failed", "crashed", "terminated")


def _matcher(rule: dict) -> dict:
    try:
        m = json.loads(rule.get("matcher_json") or "{}")
        return m if isinstance(m, dict) else {}
    except json.JSONDecodeError:
        return {}


def match_rule(rule: dict, event: dict) -> bool:
    """True when event satisfies rule's event_type and matcher subset."""
    if rule.get("event_type") and rule.get("event_type") != event.get("type"):
        return False
    m = _matcher(rule)
    for key, want in m.items():
        got = event.get(key)
        if want is not None and got != want:
            return False
    return True


def quiet_hours_active(rule: dict, now: datetime | None = None) -> bool:
    """True when `now` falls inside the rule's quiet window ('' = no limit)."""
    start = rule.get("quiet_start") or ""
    end = rule.get("quiet_end") or ""
    if not start or not end:
        return False
    now = now or datetime.now()
    try:
        sh, sm = (int(x) for x in start.split(":", 1))
        eh, em = (int(x) for x in end.split(":", 1))
    except ValueError:
        return False
    cur = now.hour * 60 + now.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s <= e:  # same-day window
        return s <= cur < e
    return cur >= s or cur < e  # wraps past midnight
```

`src/session_manager.py` 修改 — 在 `__init__` 的 `_listeners` 初始化加事件名:

```python
        self._listeners: dict[str, list[Callable[..., Any]]] = {
            "output": [], "agentEvent": [], "change": [], "remove": [],
            "session_event": [],
        }
```

（在 `_on_host_exit` 函数体末尾，`self._emit("change", ...)` 之后加:）

```python
        self._emit("session_event", {
            "type": "crashed" if session.get("exit_signal") else
                    ("completed" if session.get("exit_code") == 0 else "failed"),
            "session_id": session["id"], "name": session.get("name"),
            "tool": session.get("tool"), "project": session.get("cwd"),
            "state": "stopped", "exit_code": session.get("exit_code"),
            "signal": session.get("exit_signal"), "ts": time.time(),
        })
```

（在 agent `wait_exit` 的退出处理处，同样模式 emit `session_event`，type 按 agent 的 exit_code 判定；在 `stop()` 优雅退出后 emit `{"type": "terminated", ...}` 同 payload。）

> 注意：三处 emit 的 payload 字段完全一致（`type/session_id/name/tool/project/state/exit_code/signal/ts`），仅 type 不同。不要在 session_manager 里 import rules/notifier——事件总线由 server 接线。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_rules -v`
Expected: 7 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd /root/webpty && python3 -m unittest discover -s test 2>&1 | tail -3`
Expected: 现有测试全绿（session_manager 的 StubHost 测试仍通过，因为 emit 只是多一条事件）

```bash
git add src/rules.py src/session_manager.py test/test_rules.py
git commit -m "feat(ext): session_event hooks + rule matcher with quiet hours"
```

---

### Task 3: notifier.py + mailer.py（事件→规则→记录→SMTP）

**Files:**
- Create: `src/notifier.py`、`src/mailer.py`
- Test: `test/test_notifier.py`、`test/test_mailer.py`

**Interfaces:**
- Consumes: `Database`（Task 1 全部通知/规则方法）、`match_rule`/`quiet_hours_active`（Task 2）、config dict 的 `notify` 段（`{"smtp": {"host","port","tls","user","password","from","to"}, "default_level": "warn"}`，全部可选）
- Produces:
  - `class Notifier:` — `__init__(self, db: Database, config: dict)`; `handle_event(self, event: dict) -> None`（同步回调，内部 `asyncio.create_task`）; `async _process(self, event: dict) -> None`（规则匹配→去重→写库→触发发送）; `async send_pending(self) -> int`（重试队列，返回发送数）; `async test_message(self) -> bool`
  - `class Mailer:` — `__init__(self, config: dict)`（从 config["notify"]["smtp"] 读）; `def enabled(self) -> bool`（host 非空）; `def send(self, subject: str, html: str) -> None`（smtplib，失败抛异常由调用方记日志）

- [ ] **Step 1: 写失败测试**

`test/test_mailer.py`:

```python
import unittest
from unittest import mock

from mailer import Mailer


class MailerTest(unittest.TestCase):
    def test_disabled_when_no_host(self):
        m = Mailer({"smtp": {}})
        self.assertFalse(m.enabled())

    def test_enabled_with_host(self):
        m = Mailer({"smtp": {"host": "smtp.example.com"}})
        self.assertTrue(m.enabled())

    @mock.patch("smtplib.SMTP_SSL")
    @mock.patch("smtplib.SMTP")
    def test_send_uses_tls(self, smtp, smtp_ssl):
        cfg = {"smtp": {"host": "h", "port": 465, "tls": True,
                        "user": "u", "password": "p",
                        "from": "a@x.com", "to": "b@x.com"}}
        m = Mailer(cfg)
        m.send("subj", "<b>html</b>")
        inst = smtp_ssl.return_value
        inst.login.assert_called_once_with("u", "p")
        inst.sendmail.assert_called_once()
        args = inst.sendmail.call_args
        self.assertEqual(args.args[0], "a@x.com")
        self.assertIn("b@x.com", args.args[1])

    @mock.patch("smtplib.SMTP")
    def test_send_plaintext(self, smtp):
        cfg = {"smtp": {"host": "h", "port": 587, "tls": False,
                        "user": "", "password": "", "from": "a@x.com",
                        "to": "b@x.com"}}
        Mailer(cfg).send("s", "<p>hi</p>")
        inst = smtp.return_value
        inst.starttls.assert_not_called()
        self.assertTrue(inst.sendmail.called)


if __name__ == "__main__":
    unittest.main()
```

`test/test_notifier.py`:

```python
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from db import Database
from notifier import Notifier


class NotifierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-nf-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"notify": {"default_level": "warn"}}
        self.n = Notifier(self.db, self.cfg)

    def tearDown(self):
        self.db.close()

    def event(self, **kw):
        base = {"type": "failed", "session_id": "s1", "name": "n1",
                "tool": "claude", "project": "/p", "state": "stopped",
                "exit_code": 1, "signal": None, "ts": 1.0}
        base.update(kw)
        return base

    async def test_no_rules_still_records_warn(self):
        self.n.handle_event(self.event())
        await asyncio.sleep(0.1)
        page = await self.db.list_notifications(1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["event_type"], "failed")

    async def test_dedup_window(self):
        self.n.handle_event(self.event(ts=1000.0))
        self.n.handle_event(self.event(ts=1000.5))
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.list_notifications(1))["total"], 1)

    async def test_rule_suppressed_by_quiet_hours(self):
        await self.db.upsert_rule({
            "name": "q", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "warn",
            "quiet_start": "00:00", "quiet_end": "23:59", "enabled": 1})
        self.n.handle_event(self.event())
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.list_notifications(1))["total"], 0)

    async def test_rule_level_escalation_and_mail(self):
        await self.db.upsert_rule({
            "name": "r", "event_type": "failed",
            "matcher_json": '{"tool": "claude"}', "action": "email",
            "level": "critical", "quiet_start": "", "quiet_end": "",
            "enabled": 1})
        with mock.patch.object(self.n, "_send_mail", return_value=None) as send:
            self.n.handle_event(self.event())
            await asyncio.sleep(0.1)
            self.assertTrue(send.called)
        page = await self.db.list_notifications(1)
        self.assertEqual(page["items"][0]["level"], "critical")
        # 已发送 → delivered=1
        self.assertEqual(page["items"][0]["delivered"], 1)

    async def test_send_pending_retries_undelivered(self):
        nid = await self.db.add_notification({
            "event_type": "failed", "level": "warn", "tool": "t",
            "project": "/p", "session_id": "s9", "title": "x", "body": "b",
            "dedup_key": "k9"})
        with mock.patch.object(self.n, "_send_mail", return_value=None) as send:
            sent = await self.n.send_pending()
        self.assertEqual(sent, 1)
        rows = await self.db.query("SELECT delivered FROM notifications WHERE id=?", (nid,))
        self.assertEqual(rows[0]["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_mailer test.test_notifier -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mailer'`

- [ ] **Step 3: 实现**

`src/mailer.py`（完整）:

```python
"""SMTP delivery for the notifier (stdlib smtplib). Business-management layer."""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


class Mailer:
    def __init__(self, config: dict) -> None:
        smtp = (config.get("notify") or {}).get("smtp") or {}
        self.host = smtp.get("host") or ""
        self.port = int(smtp.get("port") or (465 if smtp.get("tls") else 587))
        self.tls = bool(smtp.get("tls"))
        self.user = smtp.get("user") or ""
        self.password = smtp.get("password") or ""
        self.from_addr = smtp.get("from") or ""
        self.to_addrs = smtp.get("to") or ""

    def enabled(self) -> bool:
        return bool(self.host)

    def _recipients(self) -> list[str]:
        if isinstance(self.to_addrs, str):
            return [a.strip() for a in self.to_addrs.split(",") if a.strip()]
        return list(self.to_addrs or [])

    def send(self, subject: str, html: str) -> None:
        if not self.enabled():
            raise RuntimeError("mailer not configured")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self._recipients())
        msg.attach(MIMEText(html, "html", "utf-8"))
        if self.tls:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=15)
        try:
            if self.user:
                smtp.login(self.user, self.password)
            smtp.sendmail(self.from_addr, self._recipients(), msg.as_string())
        finally:
            smtp.quit()
```

`src/notifier.py`（完整）:

```python
"""Task-status notifications: session_event → rule match → SQLite + SMTP.

Business-management layer. Subscribes to session_manager's `session_event`
bus events; dedups by (session_id, event_type, level) within 60s.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from mailer import Mailer
from rules import match_rule, quiet_hours_active

DEDUP_WINDOW_S = 60.0
HTML_TMPL = """<div style="font-family:sans-serif">
<h3>{title}</h3><p>{body}</p>
<pre style="background:#f5f5f5;padding:8px">{meta}</pre></div>"""


class Notifier:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config
        self.mailer = Mailer(config)
        self._tasks: set[asyncio.Task] = set()

    def handle_event(self, event: dict) -> None:
        """Sync callback for the event bus; spawns an async task."""
        task = asyncio.create_task(self._process(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, event: dict) -> None:
        rules = await self.db.list_rules()
        matched = [r for r in rules if r.get("enabled")
                   and match_rule(r, event)]
        level = event.get("level") or self.config.get("notify", {}).get(
            "default_level", "warn")
        if matched:
            level = max(matched, key=lambda r: {"info": 0, "warn": 1,
                                                "critical": 2}.get(
                r.get("level", "warn"), 1)).get("level", level)
        dedup_key = f"{event.get('session_id')}|{event.get('type')}|{level}"
        if await self.db.dedup_recent(dedup_key, DEDUP_WINDOW_S):
            return
        for rule in matched:
            if quiet_hours_active(rule):
                return
        nid = await self.db.add_notification({
            "event_type": event.get("type", "unknown"),
            "level": level,
            "tool": event.get("tool"),
            "project": event.get("project"),
            "session_id": event.get("session_id"),
            "title": f"{event.get('tool', 'session')} "
                     f"{event.get('type', 'event')}",
            "body": f"会话 {event.get('name')} 已 {event.get('type')}",
            "dedup_key": dedup_key,
        })
        wants_mail = any(r.get("action") == "email" for r in matched)
        if wants_mail:
            try:
                await self._send_mail(nid, event)
            except Exception:  # noqa: BLE001 — mail failure must not break flow
                return  # keep delivered=0 → retried by send_pending

    async def _send_mail(self, nid: int, event: dict) -> None:
        if not self.mailer.enabled():
            await self.db.mark_delivered(nid, True)
            return
        self.mailer.send(
            subject=f"[webpty] {event.get('tool')} {event.get('type')}",
            html=HTML_TMPL.format(
                title=event.get("name", "session"),
                body=event.get("type", ""),
                meta=f"project: {event.get('project')}\n"
                     f"exit_code: {event.get('exit_code')}",
            ))
        await self.db.mark_delivered(nid, True)

    async def send_pending(self) -> int:
        sent = 0
        for row in await self.db.pending_notifications():
            try:
                self.mailer.send(
                    subject=f"[webpty] {row.get('tool')} {row.get('event_type')}",
                    html=HTML_TMPL.format(title=row["title"],
                                          body=row.get("body", ""),
                                          meta=f"session: {row.get('session_id')}"))
                await self.db.mark_delivered(row["id"], True)
                sent += 1
            except Exception:  # noqa: BLE001
                continue
        return sent

    async def test_message(self) -> bool:
        if not self.mailer.enabled():
            return False
        try:
            self.mailer.send(subject="[webpty] test notification",
                             html="<p>test</p>")
            return True
        except Exception:  # noqa: BLE001
            return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_mailer test.test_notifier -v`
Expected: 4 + 6 = 10 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/mailer.py src/notifier.py test/test_mailer.py test/test_notifier.py
git commit -m "feat(ext): notifier with dedup/quiet-hours/SMTP retry queue"
```

---

### Task 4: 通知 API + server 接线

**Files:**
- Modify: `src/server.py`（`_route` 加 3 个端点 + main 里接线 notifier）
- Test: `test/test_notify_api.py`

**Interfaces:**
- Consumes: `Notifier`（Task 3）、`Database`（Task 1）
- Produces: server 端点：
  - `GET /api/notify/rules` → `{"rules": [...]}`
  - `POST /api/notify/rules`（body: rule dict，含可选 `id`）→ 201 `{"id": int}`
  - `PUT /api/notify/rules/{id}` → 200 `{"ok": true}`
  - `DELETE /api/notify/rules/{id}` → 200 `{"ok": true}`
  - `GET /api/notify/messages?page=N` → Task 1 的分页结构
  - `POST /api/notify/test` → 200 `{"ok": bool}`
- Server 构造: `Server(..., db=..., notifier=...)`；main 里 `db = Database(os.path.join(data_dir, "webpty.db")); db.connect(); notifier = Notifier(db, config); sessions.on("session_event", notifier.handle_event)`

- [ ] **Step 1: 写失败测试**

`test/test_notify_api.py`（复用 test_server.py 起服样板）:

```python
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

SRC = os.path.dirname(os.path.abspath(__file__)).replace("/test", "/src")


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class NotifyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp-napi-")
        cls.port = _pick_port()
        env = dict(os.environ)
        env.update({"WEBPTY_DATA_DIR": cls.tmp,
                    "WEBPTY_PROJECTS_ROOT": cls.tmp,
                    "WEBPTY_PORT": str(cls.port),
                    "WEBPTY_BIND_HOST": "127.0.0.1"})
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(SRC, "server.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/config",
                                       timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def test_rules_crud(self):
        st, out = self._req("GET", "/api/notify/rules")
        self.assertEqual(st, 200)
        self.assertEqual(out, {"rules": []})
        st, out = self._req("POST", "/api/notify/rules", {
            "name": "r1", "event_type": "failed", "matcher_json": "{}",
            "action": "email", "level": "critical", "quiet_start": "",
            "quiet_end": "", "enabled": 1})
        self.assertEqual(st, 201)
        rid = out["id"]
        st, rules = self._req("GET", "/api/notify/rules")
        self.assertEqual(len(rules["rules"]), 1)
        st, out = self._req("PUT", f"/api/notify/rules/{rid}",
                            {"id": rid, "name": "r1b", "event_type": "failed",
                             "matcher_json": "{}", "action": "email",
                             "level": "warn", "quiet_start": "",
                             "quiet_end": "", "enabled": 1})
        self.assertTrue(out["ok"])
        st, out = self._req("DELETE", f"/api/notify/rules/{rid}")
        self.assertTrue(out["ok"])
        st, rules = self._req("GET", "/api/notify/rules")
        self.assertEqual(rules, {"rules": []})

    def test_messages_pagination(self):
        st, out = self._req("GET", "/api/notify/messages?page=1")
        self.assertEqual(st, 200)
        self.assertIn("total", out)
        self.assertIn("items", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_notify_api -v`
Expected: FAIL — 404/405（端点未实现）

- [ ] **Step 3: 实现**

在 `src/server.py` 的 `_route` 中（`/api/sessions` 正则块之后、静态兜底之前）插入:

```python
        # --- notifications -------------------------------------------------
        if path == "/api/notify/rules" and method == "GET":
            return await self._send_json(
                writer, 200, {"rules": await self.db.list_rules()}, headers)
        if path == "/api/notify/rules" and method == "POST":
            body = await self._read_json(reader, headers)
            rid = await self.db.upsert_rule(body)
            return await self._send_json(writer, 201, {"id": rid}, headers)
        m = re.match(r"^/api/notify/rules/(\d+)$", path)
        if m and method == "PUT":
            body = await self._read_json(reader, headers)
            body["id"] = int(m.group(1))
            await self.db.upsert_rule(body)
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if m and method == "DELETE":
            await self.db.delete_rule(int(m.group(1)))
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if path == "/api/notify/messages" and method == "GET":
            page = int(self._query_param(path, "page") or 1)
            return await self._send_json(
                writer, 200, await self.db.list_notifications(page), headers)
        if path == "/api/notify/test" and method == "POST":
            ok = await self.notifier.test_message()
            return await self._send_json(writer, 200, {"ok": ok}, headers)
```

新增辅助方法（放在 `_read_json` 旁）:

```python
    @staticmethod
    def _query_param(path: str, name: str) -> str | None:
        if "?" not in path:
            return None
        for part in path.split("?", 1)[1].split("&"):
            k, _, v = part.partition("=")
            if k == name:
                return v
        return None
```

`Server.__init__` 增加参数 `db=None, notifier=None` 并存为 `self.db` / `self.notifier`；`main()` 中:

```python
    from db import Database
    from notifier import Notifier
    db = Database(os.path.join(data_dir, "webpty.db"))
    db.connect()
    notifier = Notifier(db, config)
    server = Server(config, sessions, db=db, notifier=notifier)
    sessions.on("session_event", notifier.handle_event)
```

（若 `Server.__init__` 已有多参数，按现有签名追加带默认值的参数；`data_dir` 已在 main 作用域可用。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_notify_api -v`
Expected: 2 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/server.py test/test_notify_api.py
git commit -m "feat(ext): notification REST API wired into server"
```

---

### Task 5: WebUI 通知中心面板

**Files:**
- Modify: `public/index.html`（body 末尾加 backdrop+panel 容器）
- Modify: `public/styles.css`（面板样式）
- Modify: `public/app.js`（顶部菜单加"通知中心"入口 + 面板逻辑）
- Test: 手动（curl 验证 API 已通）+ 语法检查 `node --check public/app.js`

**Interfaces:**
- Consumes: Task 4 的 3 个 API
- Produces: 前端 `openNotifyPanel()`（复用 `hidden` 弹层模式）、菜单项"通知中心"

- [ ] **Step 1: 加 HTML 容器**

`public/index.html` body 末尾（`#token-gate` 之后）:

```html
  <div id="notify-backdrop" class="panel-backdrop" hidden>
    <div class="panel">
      <div class="panel-head">
        <h2>通知中心</h2>
        <button id="notify-close" class="panel-close" type="button">×</button>
      </div>
      <div class="panel-body">
        <div class="notify-toolbar">
          <label>规则
            <select id="notify-rule-type">
              <option value="failed">failed</option>
              <option value="completed">completed</option>
              <option value="crashed">crashed</option>
              <option value="terminated">terminated</option>
            </select>
          </label>
          <button id="notify-rule-add" type="button">添加规则</button>
          <button id="notify-test" type="button">测试发送</button>
        </div>
        <div id="notify-rules" class="notify-section"></div>
        <div id="notify-messages" class="notify-section"></div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: 加 CSS**

`public/styles.css`（末尾）:

```css
.panel-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.4);
  z-index: 60; display: flex; justify-content: center; align-items: center; }
.panel-backdrop[hidden] { display: none; }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 12px; min-width: 480px; max-width: min(640px, calc(100vw - 24px));
  max-height: 80vh; display: flex; flex-direction: column; }
.panel-head { display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; border-bottom: 1px solid var(--line); }
.panel-body { padding: 12px 16px; overflow-y: auto; }
.notify-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.notify-section { margin-bottom: 14px; }
.notify-item { padding: 8px 10px; border: 1px solid var(--line);
  border-radius: 8px; margin-bottom: 6px; font-size: 13px; }
.notify-item.critical { border-color: #e5534b; }
.notify-item.warn { border-color: #d29922; }
```

- [ ] **Step 3: 加 JS 逻辑**

`public/app.js`（`const` 区加句柄，`openMenu` 的"工作流"分组后加入口）:

```js
const notifyBackdrop = document.getElementById('notify-backdrop');
const notifyRules = document.getElementById('notify-rules');
const notifyMessages = document.getElementById('notify-messages');
```

菜单（`openMenu` 中 addMenuSep 之后）:

```js
  addMenuSep();
  addMenuLabel('扩展');
  addMenuItem('通知中心', openNotifyPanel);
  addMenuItem('成本账单', () => { closeMenu(); alert('成本面板（M3 实现）'); });
  addMenuItem('备份管理', () => { closeMenu(); alert('备份面板（M4 实现）'); });
  addMenuItem('迁移向导', () => { closeMenu(); alert('迁移面板（M5 实现）'); });
```

面板逻辑（文件末尾）:

```js
function openNotifyPanel() {
  closeMenu();
  notifyBackdrop.hidden = false;
  refreshNotifyPanel();
}
async function refreshNotifyPanel() {
  const [rules, msgs] = await Promise.all([
    api('/api/notify/rules').catch(() => ({ rules: [] })),
    api('/api/notify/messages?page=1').catch(() => ({ items: [] })),
  ]);
  notifyRules.innerHTML = '<h4>规则</h4>' + (rules.rules || []).map((r) =>
    `<div class="notify-item">${esc(r.name)} — ${esc(r.event_type)}
     ${r.enabled ? '' : '(停用)'}</div>`).join('') || '<p>无规则</p>';
  notifyMessages.innerHTML = '<h4>消息记录</h4>' + (msgs.items || []).slice(0, 20).map((m) =>
    `<div class="notify-item ${esc(m.level)}">[${esc(m.level)}] ${esc(m.title)}
     <span class="muted">${esc(m.tool || '')} ${esc(m.project || '')}</span></div>`
  ).join('') || '<p>暂无消息</p>';
}
document.getElementById('notify-close').onclick = () => { notifyBackdrop.hidden = true; };
notifyBackdrop.addEventListener('click', (ev) => {
  if (ev.target === notifyBackdrop) notifyBackdrop.hidden = true;
});
document.getElementById('notify-rule-add').onclick = async () => {
  const type = document.getElementById('notify-rule-type').value;
  await api('/api/notify/rules', { method: 'POST', body: JSON.stringify({
    name: 'rule-' + Date.now(), event_type: type, matcher_json: '{}',
    action: 'email', level: 'warn', quiet_start: '', quiet_end: '', enabled: 1 }) });
  refreshNotifyPanel();
};
document.getElementById('notify-test').onclick = async () => {
  const r = await api('/api/notify/test', { method: 'POST' });
  alert(r.ok ? '测试邮件已发送' : 'SMTP 未配置');
};
```

（若 `esc()` 已存在则复用；不存在则加 `const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));`）

- [ ] **Step 4: 验证**

Run: `cd /root/webpty && node --check public/app.js`
Expected: 无语法错误

Run: `systemctl restart webpty.service && curl -s http://127.0.0.1:4790/api/notify/rules`
Expected: `{"rules":[]}`（API 在线）

- [ ] **Step 5: 提交**

```bash
git add public/index.html public/styles.css public/app.js
git commit -m "feat(ext): notification center panel (rules + messages + test)"
```

---

### Task 6: price_table.py 模型价格表

**Files:**
- Create: `src/price_table.py`
- Test: `test/test_price_table.py`

**Interfaces:**
- Consumes: config dict 的 `prices` 段（可选，形如 `{"claude": {"input": 3.0, "output": 15.0, "cache_hit": 0.3, "currency": "USD"}}`，每 1M token）
- Produces:
  - `DEFAULT_PRICES: dict` — 内置 `claude` / `codex` / `reasonix` / `opencode` / `deepseek` 默认价（每 1M token 美元：claude input 3.0 output 15.0 cache_hit 0.3；codex input 2.5 output 10.0 cache_hit 0.5；reasonix input 0.55 output 2.19 cache_hit 0.07；opencode input 0.55 output 2.19 cache_hit 0.07；deepseek input 0.27 output 1.1 cache_hit 0.07）
  - `def get_price(model: str, config: dict) -> dict` — 先查 config["prices"][model]，再查 DEFAULT_PRICES，最后返回通用默认 `{"input": 1.0, "output": 2.0, "cache_hit": 0.1, "currency": "USD"}`
  - `def cost_for(model: str, tokens_in: int, tokens_out: int, config: dict, cached_in: int = 0) -> float` — `(tokens_in - cached_in) * input/1e6 + cached_in * cache_hit/1e6 + tokens_out * output/1e6`，负数钳 0

- [ ] **Step 1: 写失败测试**

`test/test_price_table.py`:

```python
import unittest

from price_table import DEFAULT_PRICES, cost_for, get_price


class PriceTableTest(unittest.TestCase):
    def test_defaults_present(self):
        for m in ("claude", "codex", "reasonix", "opencode", "deepseek"):
            self.assertIn(m, DEFAULT_PRICES)

    def test_unknown_model_falls_back(self):
        p = get_price("mystery-model", {})
        self.assertEqual(p["input"], 1.0)
        self.assertEqual(p["output"], 2.0)

    def test_config_overrides(self):
        cfg = {"prices": {"claude": {"input": 99.0, "output": 99.0,
                                     "cache_hit": 0.0, "currency": "CNY"}}}
        p = get_price("claude", cfg)
        self.assertEqual(p["input"], 99.0)
        self.assertEqual(p["currency"], "CNY")

    def test_cost_calculation(self):
        cfg = {"prices": {"m": {"input": 10.0, "output": 20.0,
                                "cache_hit": 1.0, "currency": "USD"}}}
        c = cost_for("m", 1_000_000, 500_000, cfg)
        self.assertAlmostEqual(c, 20.0, places=6)  # 10 + 10
        c2 = cost_for("m", 1_000_000, 500_000, cfg, cached_in=1_000_000)
        self.assertAlmostEqual(c2, 11.0, places=6)  # 1 + 10

    def test_cost_clamps_negative(self):
        cfg = {"prices": {"m": {"input": 10.0, "output": 20.0,
                                "cache_hit": 1.0, "currency": "USD"}}}
        self.assertEqual(cost_for("m", 0, 0, cfg), 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_price_table -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`src/price_table.py`（完整）:

```python
"""Model price table (per 1M tokens) with built-in defaults + config override."""
from __future__ import annotations

DEFAULT_PRICES: dict[str, dict] = {
    "claude":   {"input": 3.0,  "output": 15.0, "cache_hit": 0.3,  "currency": "USD"},
    "codex":    {"input": 2.5,  "output": 10.0, "cache_hit": 0.5,  "currency": "USD"},
    "reasonix": {"input": 0.55, "output": 2.19, "cache_hit": 0.07, "currency": "USD"},
    "opencode": {"input": 0.55, "output": 2.19, "cache_hit": 0.07, "currency": "USD"},
    "deepseek": {"input": 0.27, "output": 1.1,  "cache_hit": 0.07, "currency": "USD"},
}
_FALLBACK = {"input": 1.0, "output": 2.0, "cache_hit": 0.1, "currency": "USD"}


def get_price(model: str, config: dict) -> dict:
    prices = config.get("prices") or {}
    if model in prices and isinstance(prices[model], dict):
        return prices[model]
    if model in DEFAULT_PRICES:
        return DEFAULT_PRICES[model]
    return _FALLBACK


def cost_for(model: str, tokens_in: int, tokens_out: int, config: dict,
             cached_in: int = 0) -> float:
    p = get_price(model, config)
    fresh = max(tokens_in - max(cached_in, 0), 0)
    total = (fresh * float(p.get("input", 1.0))
             + max(cached_in, 0) * float(p.get("cache_hit", 0.1))
             + tokens_out * float(p.get("output", 2.0))) / 1_000_000.0
    return max(total, 0.0)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_price_table -v`
Expected: 5 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/price_table.py test/test_price_table.py
git commit -m "feat(ext): model price table with defaults + config override"
```

---

### Task 7: usage_parser.py 用量解析

**Files:**
- Create: `src/usage_parser.py`
- Test: `test/test_usage_parser.py`

**Interfaces:**
- Consumes: stream-json 行（claude 的 `message_start`/`message_delta` usage；reasonix/opencode 的统计事件；通用 `{"usage": {"input_tokens","output_tokens","input_tokens_cached"}}`）
- Produces: `def parse_usage(line: str, tool: str) -> dict | None` — 返回 `{"tokens_in", "tokens_out", "cached_in", "cost", "model", "session_id"}` 或 None（无法解析时 None → 走 posthoc 兜底）

- [ ] **Step 1: 写失败测试**

`test/test_usage_parser.py`:

```python
import unittest

from usage_parser import parse_usage


class UsageParserTest(unittest.TestCase):
    def test_claude_message_start(self):
        line = json_line({"type": "message_start",
                          "message": {"usage": {"input_tokens": 100,
                                                "cache_creation_input_tokens": 50}}})
        u = parse_usage(line, "claude")
        self.assertIsNotNone(u)
        self.assertEqual(u["tokens_in"], 100)
        self.assertEqual(u["cached_in"], 50)

    def test_claude_message_delta(self):
        line = json_line({"type": "message_delta",
                          "usage": {"output_tokens": 200}})
        u = parse_usage(line, "claude")
        self.assertEqual(u["tokens_out"], 200)

    def test_generic_usage_event(self):
        line = json_line({"type": "usage",
                          "usage": {"input_tokens": 10, "output_tokens": 20,
                                    "input_tokens_cached": 3}})
        u = parse_usage(line, "reasonix")
        self.assertEqual((u["tokens_in"], u["tokens_out"], u["cached_in"]),
                         (10, 20, 3))

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_usage("not json at all", "claude"))
        self.assertIsNone(parse_usage('{"type": "ping"}', "claude"))

    def test_reasonix_stats_event(self):
        line = json_line({"type": "stats", "model": "deepseek-v4-flash",
                          "tokens_in": 5, "tokens_out": 6})
        u = parse_usage(line, "reasonix")
        self.assertIsNotNone(u)
        self.assertEqual(u["model"], "deepseek-v4-flash")


def json_line(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_usage_parser -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`src/usage_parser.py`（完整）:

```python
"""Parse token usage from agent stream-json lines (realtime source).

Returns None when the line carries no usage → the reconciler picks it up
later from logs (posthoc source). Business-management layer.
"""
from __future__ import annotations

import json
from typing import Any


def _extract(usage: dict) -> dict:
    return {
        "tokens_in": int(usage.get("input_tokens") or 0),
        "tokens_out": int(usage.get("output_tokens") or 0),
        "cached_in": int(usage.get("input_tokens_cached")
                            or usage.get("cache_creation_input_tokens") or 0),
    }


def parse_usage(line: str, tool: str) -> dict | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if isinstance(usage, dict):
        u = _extract(usage)
        if u["tokens_in"] or u["tokens_out"]:
            return {
                **u,
                "cost": None,  # computed by cost_tracker via price_table
                "model": obj.get("model") or obj.get("message", {}).get("model"),
                "session_id": obj.get("session_id"),
            }
    if obj.get("type") in ("stats", "usage_event") and (
            obj.get("tokens_in") is not None or obj.get("tokens_out") is not None):
        return {
            "tokens_in": int(obj.get("tokens_in") or 0),
            "tokens_out": int(obj.get("tokens_out") or 0),
            "cached_in": int(obj.get("cached_in") or 0),
            "cost": obj.get("cost"),
            "model": obj.get("model"),
            "session_id": obj.get("session_id"),
        }
    return None
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_usage_parser -v`
Expected: 5 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/usage_parser.py test/test_usage_parser.py
git commit -m "feat(ext): stream-json usage parser (claude/reasonix/generic)"
```

---

### Task 8: cost_tracker.py 实时计量

**Files:**
- Create: `src/cost_tracker.py`
- Test: `test/test_cost_tracker.py`

**Interfaces:**
- Consumes: `Database`（usage 方法）、`parse_usage`（Task 7）、`cost_for`（Task 6）、config dict
- Produces:
  - `class CostTracker:` — `__init__(self, db, config)`; `handle_agent_event(self, event: dict) -> None`（同步回调订阅 `agentEvent`；event 形如 `{"type": "result", "raw": "<stream-json line>", "session_id": ..., "tool": ...}` 或自带 `usage` 字段）; `async _record(self, event: dict) -> None`; `async summary(self, period) -> dict`; `async grouped(self, group, period) -> list[dict]`; `async alerts(self) -> list[dict]`; `async set_budget(self, limit: float) -> None`; `async over_budget(self) -> bool`
- server 接线: `sessions.on("agentEvent", cost.handle_agent_event)`（与 Task 4 的 notifier 接线同处）

- [ ] **Step 1: 写失败测试**

`test/test_cost_tracker.py`:

```python
import asyncio
import json
import os
import tempfile
import unittest

from cost_tracker import CostTracker
from db import Database


class CostTrackerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-cost-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"prices": {"claude": {"input": 10.0, "output": 20.0,
                                          "cache_hit": 1.0, "currency": "USD"}}}
        self.c = CostTracker(self.db, self.cfg)

    def tearDown(self):
        self.db.close()

    def ev(self, line, sid="s1", tool="claude"):
        return {"type": "result", "raw": line, "session_id": sid, "tool": tool}

    async def test_records_from_raw_stream_json(self):
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 500}})
        self.c.handle_agent_event(self.ev(line))
        await asyncio.sleep(0.1)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 500)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 500*20/1e6

    async def test_records_from_embedded_usage(self):
        self.c.handle_agent_event({"type": "result", "session_id": "s2",
                                   "tool": "claude",
                                   "usage": {"input_tokens": 1000,
                                             "output_tokens": 0}})
        await asyncio.sleep(0.1)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_in"], 1000)
        self.assertAlmostEqual(s["cost"], 0.01, places=6)  # 1000*10/1e6

    async def test_ignores_unparseable(self):
        self.c.handle_agent_event(self.ev("garbage"))
        await asyncio.sleep(0.1)
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 0)

    async def test_budget_alerts(self):
        await self.c.set_budget(0.001)
        line = json.dumps({"type": "message_delta",
                           "usage": {"output_tokens": 1000}})
        self.c.handle_agent_event(self.ev(line))
        await asyncio.sleep(0.1)
        self.assertTrue(await self.c.over_budget())

    async def test_summary_and_grouped(self):
        for i, tool in enumerate(("claude", "codex")):
            self.c.handle_agent_event(self.ev(
                json.dumps({"type": "message_delta",
                            "usage": {"output_tokens": 100}}),
                sid=f"s{i}", tool=tool))
        await asyncio.sleep(0.1)
        g = await self.c.grouped("tool", "day")
        self.assertEqual(len(g), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_cost_tracker -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`src/cost_tracker.py`（完整）:

```python
"""Realtime token & cost tracking from agent stream-json events.

Subscribes to session_manager's `agentEvent` bus; parses usage, computes
cost via price_table, persists to token_usage (source=realtime) and keeps
a budget limit. Business-management layer.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from price_table import cost_for
from usage_parser import parse_usage


class CostTracker:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config
        self._budget: float = float(
            (config.get("budget") or {}).get("limit", 0.0) or 0.0)
        self._tasks: set[asyncio.Task] = set()

    def handle_agent_event(self, event: dict) -> None:
        task = asyncio.create_task(self._record(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record(self, event: dict) -> None:
        usage = None
        if event.get("usage") is not None:
            u = event["usage"]
            usage = {
                "tokens_in": int(u.get("input_tokens") or 0),
                "tokens_out": int(u.get("output_tokens") or 0),
                "cached_in": int(u.get("input_tokens_cached") or 0),
                "cost": u.get("cost"), "model": u.get("model"),
                "session_id": event.get("session_id"),
            }
        else:
            raw = event.get("raw")
            if isinstance(raw, str):
                usage = parse_usage(raw, event.get("tool") or "")
            elif isinstance(raw, dict):
                usage = parse_usage(json.dumps(raw), event.get("tool") or "")
        if not usage:
            return
        model = usage.get("model") or event.get("tool") or "unknown"
        cost = usage.get("cost")
        if cost is None:
            cost = cost_for(model, usage["tokens_in"], usage["tokens_out"],
                            self.config, cached_in=usage.get("cached_in", 0))
        await self.db.add_usage({
            "project": event.get("project"),
            "tool": event.get("tool"),
            "model": model,
            "session_id": usage.get("session_id") or event.get("session_id"),
            "tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"],
            "cost": cost, "source": "realtime"})

    async def summary(self, period: str) -> dict:
        return await self.db.usage_summary(period)

    async def grouped(self, group: str, period: str) -> list[dict]:
        return await self.db.usage_grouped(group, period)

    async def alerts(self) -> list[dict]:
        over = await self.over_budget()
        return [{"type": "budget", "level": "critical",
                 "title": "预算超限", "active": over,
                 "budget": self._budget}]

    async def set_budget(self, limit: float) -> None:
        self._budget = max(float(limit), 0.0)
        budget = dict(self.config.get("budget") or {})
        budget["limit"] = self._budget
        self.config["budget"] = budget

    async def over_budget(self) -> bool:
        if self._budget <= 0:
            return False
        s = await self.db.usage_summary("month")
        return s["cost"] > self._budget
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_cost_tracker -v`
Expected: 5 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/cost_tracker.py test/test_cost_tracker.py
git commit -m "feat(ext): realtime cost tracker (agentEvent → token_usage)"
```

---

### Task 9: reconciler.py 事后校对 + 成本 API

**Files:**
- Create: `src/reconciler.py`
- Modify: `src/server.py`（成本端点 + 接线）
- Test: `test/test_reconciler.py`、`test/test_cost_api.py`

**Interfaces:**
- Consumes: `Database`、`parse_usage`（Task 7）、`cost_for`（Task 6）
- Produces:
  - `def scan_claude_logs(projects_dir: str) -> list[dict]` — 扫描 `*.jsonl` 文件，逐行 `parse_usage`，返回去重后的用量列表（含 `session_id` 从文件名推导）
  - `class Reconciler:` — `__init__(self, db, config)`; `async reconcile(self, projects_dir: str, tool: str = "claude") -> int`（补录 source=posthoc，按 session_id+ts 去重，返回补录数）
  - server 端点：
    - `GET /api/cost/summary?period=day|week|month`
    - `GET /api/cost/by-project|by-tool|by-model|by-session?period=`
    - `GET /api/cost/alerts`
    - `PUT /api/cost/budget`（body `{"limit": float}`）
    - `POST /api/cost/reconcile`（触发一次校对，返回 `{"added": int}`）

- [ ] **Step 1: 写失败测试**

`test/test_reconciler.py`:

```python
import asyncio
import json
import os
import tempfile
import unittest

from db import Database
from reconciler import Reconciler, scan_claude_logs


class ReconcilerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-rec-")
        self.db = Database(os.path.join(self.tmp, "webpty.db"))
        self.db.connect()
        self.cfg = {"prices": {"claude": {"input": 10.0, "output": 20.0,
                                          "cache_hit": 1.0, "currency": "USD"}}}
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.projects, "proj-a"))
        with open(os.path.join(self.projects, "proj-a", "session-x.jsonl"), "w") as f:
            f.write(json.dumps({"type": "message_delta",
                                "usage": {"output_tokens": 100}}) + "\n")
            f.write("garbage line\n")
            f.write(json.dumps({"type": "message_delta",
                                "usage": {"output_tokens": 50}}) + "\n")

    def tearDown(self):
        self.db.close()

    def test_scan_claude_logs(self):
        items = scan_claude_logs(self.projects)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["tokens_out"] > 0 for i in items))

    async def test_reconcile_persists_posthoc(self):
        r = Reconciler(self.db, self.cfg)
        added = await r.reconcile(self.projects)
        self.assertEqual(added, 2)
        s = await self.db.usage_summary("day")
        self.assertEqual(s["tokens_out"], 150)
        self.assertAlmostEqual(s["cost"], 0.003, places=6)  # 150*20/1e6

    async def test_reconcile_idempotent(self):
        r = Reconciler(self.db, self.cfg)
        await r.reconcile(self.projects)
        added2 = await r.reconcile(self.projects)
        self.assertEqual(added2, 0)
        self.assertEqual((await self.db.usage_summary("day"))["entries"], 2)


if __name__ == "__main__":
    unittest.main()
```

`test/test_cost_api.py`（起服样板同 test_notify_api.py，仅测端点）:

```python
    def test_cost_summary_and_grouped(self):
        st, out = self._req("GET", "/api/cost/summary?period=day")
        self.assertEqual(st, 200)
        self.assertIn("cost", out)
        st, out = self._req("GET", "/api/cost/by-tool?period=day")
        self.assertEqual(st, 200)
        self.assertIsInstance(out, list)

    def test_budget_roundtrip(self):
        st, out = self._req("PUT", "/api/cost/budget", {"limit": 12.5})
        self.assertTrue(out["ok"])
        st, out = self._req("GET", "/api/cost/alerts")
        self.assertIsInstance(out, list)

    def test_reconcile_runs(self):
        st, out = self._req("POST", "/api/cost/reconcile")
        self.assertEqual(st, 200)
        self.assertIn("added", out)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_reconciler test.test_cost_api -v`
Expected: FAIL — 模块不存在 / 端点 404

- [ ] **Step 3: 实现**

`src/reconciler.py`（完整）:

```python
"""Post-hoc usage reconciliation: scan agent log dirs and backfill records
that the realtime parser missed (source=posthoc). Business-management layer.
"""
from __future__ import annotations

import json
import os
from typing import Any

from price_table import cost_for
from usage_parser import parse_usage


def scan_claude_logs(projects_dir: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(projects_dir):
        return out
    for root, _dirs, files in os.walk(projects_dir):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            session_id = fn[:-6]  # strip .jsonl
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        u = parse_usage(line, "claude")
                        if u:
                            u["session_id"] = session_id
                            u["project"] = root
                            out.append(u)
            except OSError:
                continue
    return out


class Reconciler:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config

    async def reconcile(self, projects_dir: str, tool: str = "claude") -> int:
        added = 0
        for u in scan_claude_logs(projects_dir):
            key = (u.get("session_id") or "", u["tokens_in"], u["tokens_out"])
            dup = await self.db.query_one(
                "SELECT 1 AS x FROM token_usage WHERE session_id=? AND "
                "tokens_in=? AND tokens_out=? AND source='posthoc' LIMIT 1",
                key)
            if dup:
                continue
            model = u.get("model") or tool
            cost = u.get("cost")
            if cost is None:
                cost = cost_for(model, u["tokens_in"], u["tokens_out"],
                                self.config, cached_in=u.get("cached_in", 0))
            await self.db.add_usage({
                "project": u.get("project"), "tool": tool, "model": model,
                "session_id": u.get("session_id"),
                "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"],
                "cost": cost, "source": "posthoc"})
            added += 1
        return added
```

`src/server.py` 成本端点（通知端点后插入）+ 接线:

```python
        # --- cost -----------------------------------------------------------
        if path.startswith("/api/cost/summary") and method == "GET":
            period = self._query_param(path, "period") or "day"
            return await self._send_json(
                writer, 200, await self.cost.summary(period), headers)
        m = re.match(r"^/api/cost/by-(project|tool|model|session)$", path)
        if m and method == "GET":
            period = self._query_param(path, "period") or "day"
            rows = await self.cost.grouped(m.group(1), period)
            return await self._send_json(writer, 200, rows, headers)
        if path == "/api/cost/alerts" and method == "GET":
            return await self._send_json(
                writer, 200, await self.cost.alerts(), headers)
        if path == "/api/cost/budget" and method == "PUT":
            body = await self._read_json(reader, headers)
            await self.cost.set_budget(float(body.get("limit", 0)))
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if path == "/api/cost/reconcile" and method == "POST":
            import os as _os
            from reconciler import Reconciler
            claude_dir = _os.path.expanduser("~/.claude/projects")
            rec = Reconciler(self.db, self.config)
            added = await rec.reconcile(claude_dir)
            return await self._send_json(writer, 200, {"added": added}, headers)
```

`Server.__init__` 增加 `cost=None`；main 中:

```python
    from cost_tracker import CostTracker
    cost = CostTracker(db, config)
    server = Server(config, sessions, db=db, notifier=notifier, cost=cost)
    sessions.on("agentEvent", cost.handle_agent_event)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_reconciler test.test_cost_api -v`
Expected: 3 + 3 = 6 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/reconciler.py src/server.py test/test_reconciler.py test/test_cost_api.py
git commit -m "feat(ext): posthoc reconciler + cost REST API"
```

---

### Task 10: WebUI 成本账单面板

**Files:**
- Modify: `public/index.html`（加面板容器）
- Modify: `public/styles.css`
- Modify: `public/app.js`（菜单入口改为真面板）
- Test: `node --check public/app.js` + curl

**Interfaces:**
- Consumes: Task 9 的成本 API
- Produces: `openCostPanel()`、菜单"成本账单"替换 Task 5 的 alert 占位

- [ ] **Step 1: HTML 容器**

```html
  <div id="cost-backdrop" class="panel-backdrop" hidden>
    <div class="panel">
      <div class="panel-head">
        <h2>成本账单</h2>
        <button id="cost-close" class="panel-close" type="button">×</button>
      </div>
      <div class="panel-body">
        <div class="notify-toolbar">
          <label>周期
            <select id="cost-period">
              <option value="day">今日</option>
              <option value="week">本周</option>
              <option value="month">本月</option>
            </select>
          </label>
          <label>预算 $
            <input id="cost-budget" type="number" step="0.01" min="0" style="width:90px">
          </label>
          <button id="cost-budget-set" type="button">设置</button>
          <button id="cost-reconcile" type="button">日志校对</button>
        </div>
        <div id="cost-cards" class="cost-cards"></div>
        <div id="cost-groups" class="notify-section"></div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: CSS**

```css
.cost-cards { display: flex; gap: 10px; margin-bottom: 12px; }
.cost-card { flex: 1; border: 1px solid var(--line); border-radius: 8px;
  padding: 10px; text-align: center; }
.cost-card .v { font-size: 20px; font-weight: 700; }
```

- [ ] **Step 3: JS**

```js
const costBackdrop = document.getElementById('cost-backdrop');
function openCostPanel() { closeMenu(); costBackdrop.hidden = false; refreshCostPanel(); }
async function refreshCostPanel() {
  const period = document.getElementById('cost-period').value;
  const [sum, byTool, alerts] = await Promise.all([
    api(`/api/cost/summary?period=${period}`).catch(() => ({})),
    api(`/api/cost/by-tool?period=${period}`).catch(() => []),
    api('/api/cost/alerts').catch(() => []),
  ]);
  document.getElementById('cost-cards').innerHTML =
    `<div class="cost-card"><div class="v">$${Number(sum.cost || 0).toFixed(4)}</div><div>成本</div></div>` +
    `<div class="cost-card"><div class="v">${sum.tokens_in || 0}</div><div>输入 tokens</div></div>` +
    `<div class="cost-card"><div class="v">${sum.tokens_out || 0}</div><div>输出 tokens</div></div>` +
    (alerts.some((a) => a.active) ? `<div class="cost-card" style="border-color:#e5534b"><div class="v">超限</div><div>预算</div></div>` : '');
  document.getElementById('cost-groups').innerHTML = '<h4>按工具</h4>' +
    (byTool || []).map((g) => `<div class="notify-item">${esc(g.name)} —
      $${Number(g.cost || 0).toFixed(4)} (${g.tokens_in}/${g.tokens_out})</div>`).join('') ||
    '<p>暂无数据</p>';
}
document.getElementById('cost-close').onclick = () => { costBackdrop.hidden = true; };
costBackdrop.addEventListener('click', (ev) => {
  if (ev.target === costBackdrop) costBackdrop.hidden = true;
});
document.getElementById('cost-period').onchange = refreshCostPanel;
document.getElementById('cost-budget-set').onclick = async () => {
  const v = parseFloat(document.getElementById('cost-budget').value || '0');
  await api('/api/cost/budget', { method: 'PUT', body: JSON.stringify({ limit: v }) });
  refreshCostPanel();
};
document.getElementById('cost-reconcile').onclick = async () => {
  const r = await api('/api/cost/reconcile', { method: 'POST' });
  alert(`日志校对完成，补录 ${r.added} 条`);
  refreshCostPanel();
};
```

菜单"成本账单"改 `addMenuItem('成本账单', openCostPanel);`

- [ ] **Step 4: 验证**

Run: `cd /root/webpty && node --check public/app.js && systemctl restart webpty.service && curl -s "http://127.0.0.1:4790/api/cost/summary?period=day"`
Expected: `{"tokens_in":0,"tokens_out":0,"cost":0.0,"entries":0}`

- [ ] **Step 5: 提交**

```bash
git add public/index.html public/styles.css public/app.js
git commit -m "feat(ext): cost dashboard panel (summary/cards/budget/reconcile)"
```

---

### Task 11: backup.py 快照/加密/轮转

**Files:**
- Create: `src/backup.py`
- Test: `test/test_backup.py`

**Interfaces:**
- Consumes: `Database`（backups 方法）、config dict（`backup` 段：`retention` 默认 7、`interval_hours` 默认 24、`encryption_key` 可选）、`data_dir`
- Produces:
  - `def collect_state(data_dir: str, config: dict, db) -> dict` — 返回 `{"config": config, "notify_rules": [...], "sessions": [...], "prices": config.get("prices", {})}`（sessions 从 `data_dir/config.json` 的 `sessions` 字段；rules 从 db）
  - `def create_backup(data_dir: str, config: dict, db) -> dict` — 打包 `data/backups/webpty-<YYYYmmdd-HHMMSS>.tar.gz`（内含 `manifest.json`），SHA256 校验，db 记录，返回 backup dict（含 `id/filename/sha256/size_bytes`）
  - `async def create_backup_async(...)` 同签名（db 调用是 async）
  - `async def list_backups(db) -> list[dict]`
  - `async def restore_backup(backup_id: int, data_dir: str, db) -> dict` — 解包校验 SHA256 → 写回 config.json（merge 语义：保留现有 + 新增 rules），返回 `{"ok": bool, "message": str}`
  - `async def diff_backups(a_id: int, b_id: int, db) -> list[dict]` — 对比两 manifest 的 config 顶层键差异，返回 `[{"key", "a", "b"}]`
  - `async def rotate(db, retention: int = 7) -> list[int]` — 删除超限记录，返回删除的 id 列表（同时删文件）

- [ ] **Step 1: 写失败测试**

`test/test_backup.py`:

```python
import asyncio
import hashlib
import json
import os
import tarfile
import tempfile
import unittest

from backup import (collect_state, create_backup_async, diff_backups,
                    list_backups, restore_backup, rotate)
from db import Database


class BackupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-bak-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "backups"))
        cfg_path = os.path.join(self.data, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"port": 4790, "sessions": [{"id": "s1", "name": "n1"}]}, f)
        self.db = Database(os.path.join(self.data, "webpty.db"))
        self.db.connect()
        self.config = {"port": 4790, "sessions": [{"id": "s1", "name": "n1"}],
                       "backup": {"retention": 2}}

    def tearDown(self):
        self.db.close()

    async def test_create_backup_makes_tar_with_manifest(self):
        b = await create_backup_async(self.data, self.config, self.db)
        self.assertTrue(b["sha256"])
        path = os.path.join(self.data, "backups", b["filename"])
        self.assertTrue(os.path.exists(path))
        with tarfile.open(path) as tf:
            names = tf.getnames()
        self.assertIn("manifest.json", names)
        with open(path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), b["sha256"])
        self.assertEqual(len(await list_backups(self.db)), 1)

    async def test_restore_roundtrip(self):
        b = await create_backup_async(self.data, self.config, self.db)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"port": 9999}, f)  # 破坏配置
        res = await restore_backup(b["id"], self.data, self.db, self.config)
        self.assertTrue(res["ok"])
        with open(os.path.join(self.data, "config.json")) as f:
            restored = json.load(f)
        self.assertEqual(restored["port"], 4790)
        # merge 保留现有 sessions
        self.assertIn("sessions", restored)

    async def test_rotate_keeps_retention(self):
        for _ in range(4):
            await create_backup_async(self.data, self.config, self.db)
        deleted = await rotate(self.db, 2)
        self.assertEqual(len(deleted), 2)
        remaining = await list_backups(self.db)
        self.assertEqual(len(remaining), 2)
        for b in remaining:
            self.assertFalse(os.path.exists(
                os.path.join(self.data, "backups", b["filename"])) is False or
                os.path.exists(os.path.join(self.data, "backups", b["filename"])))

    async def test_diff_backups(self):
        a = await create_backup_async(self.data, self.config, self.db)
        self.config["port"] = 4800
        b = await create_backup_async(self.data, self.config, self.db)
        diff = await diff_backups(a["id"], b["id"], self.db)
        self.assertTrue(any(d["key"] == "port" for d in diff))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_backup -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`src/backup.py`（完整）:

```python
"""Automatic configuration backups: snapshot → tar.gz + manifest + SHA256,
optional AES-GCM encryption (only when `cryptography` is importable),
retention rotation. Business-management layer.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time

from db import Database


def _manifest(data_dir: str, config: dict, rules: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "created_at": time.time(),
        "content": ["config.json", "notify_rules", "sessions"],
        "sha256": "",
        "size_bytes": 0,
    }


async def collect_state(data_dir: str, config: dict, db: Database) -> dict:
    rules = await db.list_rules()
    cfg_path = os.path.join(data_dir, "config.json")
    sessions = []
    try:
        with open(cfg_path, encoding="utf-8") as f:
            stored = json.load(f)
        sessions = stored.get("sessions", []) if isinstance(stored, dict) else []
    except (OSError, json.JSONDecodeError):
        sessions = []
    return {"config": config, "notify_rules": rules, "sessions": sessions,
            "prices": config.get("prices", {})}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _maybe_encrypt(data: bytes, config: dict) -> tuple[bytes, bool]:
    key = (config.get("backup") or {}).get("encryption_key") or ""
    if not key:
        return data, False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return data, False
    nonce = os.urandom(12)
    ct = AESGCM(key.encode()[:32].ljust(32, b"\0")).encrypt(
        nonce, data, None)
    return nonce + ct, True


async def create_backup_async(data_dir: str, config: dict, db: Database) -> dict:
    backups_dir = os.path.join(data_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    state = await collect_state(data_dir, config, db)
    manifest = _manifest(data_dir, config, state["notify_rules"])
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    payload, encrypted = _maybe_encrypt(raw, config)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = payload if encrypted else raw
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    filename = f"webpty-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    path = os.path.join(backups_dir, filename)
    with open(path, "wb") as f:
        f.write(blob)
    sha = hashlib.sha256(blob).hexdigest()
    bid = await db.add_backup({
        "filename": filename, "size_bytes": len(blob), "sha256": sha,
        "manifest_json": json.dumps(manifest, sort_keys=True),
        "encrypted": 1 if encrypted else 0, "retained": 1})
    manifest["sha256"] = sha
    manifest["size_bytes"] = len(blob)
    return {"id": bid, "filename": filename, "sha256": sha,
            "size_bytes": len(blob), "encrypted": encrypted}


async def list_backups(db: Database) -> list[dict]:
    return await db.list_backups()


async def restore_backup(backup_id: int, data_dir: str, db: Database,
                         config: dict | None = None) -> dict:
    row = await db.get_backup(backup_id)
    if not row:
        return {"ok": False, "message": "backup not found"}
    path = os.path.join(data_dir, "backups", row["filename"])
    if not os.path.exists(path):
        return {"ok": False, "message": "file missing"}
    if _sha256_file(path) != row["sha256"]:
        return {"ok": False, "message": "sha256 mismatch"}
    with tarfile.open(path, "r:gz") as tf:
        member = tf.getmember("manifest.json")
        raw = tf.extractfile(member).read()
    if row.get("encrypted"):
        key = (config or {}).get("backup", {}).get("encryption_key") or ""
        if not key:
            return {"ok": False, "message": "backup is encrypted, key missing"}
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return {"ok": False, "message": "cryptography not installed"}
        nonce, ct = raw[:12], raw[12:]
        raw = AESGCM(key.encode()[:32].ljust(32, b"\0")).decrypt(nonce, ct, None)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "message": "corrupt manifest"}
    cfg = state.get("config") or {}
    cfg_path = os.path.join(data_dir, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}
    merged = dict(existing)
    merged.update(cfg)  # merge 语义：备份覆盖冲突键，保留现有新增键
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return {"ok": True, "message": "restored"}


async def diff_backups(a_id: int, b_id: int, db: Database) -> list[dict]:
    async def _load(bid: int) -> dict:
        """Read the state dict from a backup package by id."""
        row = await db.get_backup(bid)
        if not row:
            return {}
        path = os.path.join(os.path.dirname(db.path), "backups", row["filename"])
        if not os.path.exists(path):
            return {}
        with tarfile.open(path, "r:gz") as tf:
            return json.loads(tf.extractfile("manifest.json").read())

    a = await db.get_backup(a_id)
    b = await db.get_backup(b_id)
    if not a or not b:
        return [{"key": "_error", "a": "missing", "b": "missing"}]
    sa = await _load(a_id)
    sb = await _load(b_id)
    keys = set(sa.keys()) | set(sb.keys())
    return [{"key": k, "a": sa.get(k), "b": sb.get(k)}
            for k in sorted(keys) if sa.get(k) != sb.get(k)]


async def rotate(db: Database, retention: int = 7) -> list[int]:
    rows = await db.list_backups()
    if len(rows) <= retention:
        return []
    doomed = rows[retention:]
    deleted = []
    for row in doomed:
        await db.delete_backup(row["id"])
        deleted.append(row["id"])
    return deleted
```

> 注：`diff_backups` 用嵌套 async `_load` 读取两包的 manifest 对比；加密包（encrypted=1）在 restore/diff 前需用配置密钥解密——Task 11 阶段加密为可选增强，测试用未加密包，解密逻辑在 Task 18 收尾时补充验证。

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_backup -v`
Expected: 4 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/backup.py test/test_backup.py
git commit -m "feat(ext): backup snapshots with sha256/optional-encryption/rotation"
```

---

### Task 12: 备份 API + 定时任务

**Files:**
- Modify: `src/server.py`（备份端点 + 定时 task）
- Test: `test/test_backup_api.py`

**Interfaces:**
- Consumes: Task 11 的 `create_backup_async/list_backups/restore_backup/diff_backups/rotate`
- Produces: 端点：
  - `POST /api/backup/create` → 201 `{"backup": {...}}`
  - `GET /api/backup/list` → `{"backups": [...]}`
  - `POST /api/backup/restore/{id}` → 200 `{"ok": bool, "message": str}`
  - `GET /api/backup/diff/{a}/{b}` → 200 `[...]`
- 定时：main 里 `create_task(_backup_loop(db, config))` — 每 `backup.interval_hours`（默认 24h）`create_backup_async` + `rotate`；启动后 30s 首次执行（测试可等待）；循环 `while True: await asyncio.sleep(interval*3600)`，异常捕获记日志不退出

- [ ] **Step 1: 写失败测试**

`test/test_backup_api.py`（起服样板同前）:

```python
    def test_backup_create_and_list(self):
        st, out = self._req("POST", "/api/backup/create")
        self.assertEqual(st, 201)
        b = out["backup"]
        self.assertTrue(b["sha256"])
        st, out = self._req("GET", "/api/backup/list")
        self.assertEqual(len(out["backups"]), 1)

    def test_backup_restore_missing_returns_ok_false(self):
        st, out = self._req("POST", "/api/backup/restore/9999")
        self.assertEqual(st, 200)
        self.assertFalse(out["ok"])
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_backup_api -v`
Expected: FAIL — 404

- [ ] **Step 3: 实现**

server.py 端点（成本端点后）:

```python
        # --- backups --------------------------------------------------------
        if path == "/api/backup/create" and method == "POST":
            b = await create_backup_async(data_dir, self.config, self.db)
            return await self._send_json(writer, 201, {"backup": b}, headers)
        if path == "/api/backup/list" and method == "GET":
            return await self._send_json(
                writer, 200, {"backups": await list_backups(self.db)}, headers)
        m = re.match(r"^/api/backup/restore/(\d+)$", path)
        if m and method == "POST":
            res = await restore_backup(int(m.group(1)), data_dir, self.db,
                                     self.config)
            return await self._send_json(writer, 200, res, headers)
        m = re.match(r"^/api/backup/diff/(\d+)/(\d+)$", path)
        if m and method == "GET":
            diff = await diff_backups(int(m.group(1)), int(m.group(2)), self.db)
            return await self._send_json(writer, 200, diff, headers)
```

（`data_dir` 需在模块级或 Server 属性；若 `_route` 无 data_dir，在 `Server.__init__` 加 `self.data_dir = data_dir` 并在 main 传入。）

main 中定时 task:

```python
    async def _backup_loop():
        from backup import create_backup_async, rotate
        interval = float((config.get("backup") or {}).get("interval_hours", 24))
        retention = int((config.get("backup") or {}).get("retention", 7))
        await asyncio.sleep(30)  # 启动后 30s 首次
        while True:
            try:
                await create_backup_async(data_dir, config, db)
                await rotate(db, retention)
            except Exception as err:  # noqa: BLE001
                log_error("backup", err)
            await asyncio.sleep(max(interval, 0.1) * 3600)

    asyncio.create_task(_backup_loop())
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_backup_api -v`
Expected: 2 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/server.py test/test_backup_api.py
git commit -m "feat(ext): backup REST API + hourly auto-backup loop"
```

---

### Task 13: WebUI 备份管理面板

**Files:**
- Modify: `public/index.html`、`public/styles.css`、`public/app.js`
- Test: `node --check` + curl

**Interfaces:**
- Consumes: Task 12 API
- Produces: `openBackupPanel()`、菜单"备份管理"（替换 Task 5 alert 占位）

- [ ] **Step 1: HTML**

```html
  <div id="backup-backdrop" class="panel-backdrop" hidden>
    <div class="panel">
      <div class="panel-head">
        <h2>备份管理</h2>
        <button id="backup-close" class="panel-close" type="button">×</button>
      </div>
      <div class="panel-body">
        <div class="notify-toolbar">
          <button id="backup-create" type="button">立即备份</button>
        </div>
        <div id="backup-list" class="notify-section"></div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: CSS** — 复用 `.panel-*` / `.notify-item`，无需新增

- [ ] **Step 3: JS**

```js
const backupBackdrop = document.getElementById('backup-backdrop');
function openBackupPanel() { closeMenu(); backupBackdrop.hidden = false; refreshBackupPanel(); }
async function refreshBackupPanel() {
  const r = await api('/api/backup/list').catch(() => ({ backups: [] }));
  document.getElementById('backup-list').innerHTML = '<h4>快照列表</h4>' +
    (r.backups || []).map((b) =>
      `<div class="notify-item">${esc(b.filename)} — ${(b.size_bytes / 1024).toFixed(1)}KB
       <button data-restore="${b.id}" type="button">恢复</button></div>`).join('') ||
    '<p>暂无备份</p>';
  document.querySelectorAll('[data-restore]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('恢复该备份将覆盖当前配置，继续？')) return;
      const r = await api(`/api/backup/restore/${btn.dataset.restore}`, { method: 'POST' });
      alert(r.ok ? '恢复成功' : '恢复失败: ' + r.message);
      refreshBackupPanel();
    };
  });
}
document.getElementById('backup-close').onclick = () => { backupBackdrop.hidden = true; };
backupBackdrop.addEventListener('click', (ev) => {
  if (ev.target === backupBackdrop) backupBackdrop.hidden = true;
});
document.getElementById('backup-create').onclick = async () => {
  await api('/api/backup/create', { method: 'POST' });
  refreshBackupPanel();
};
```

菜单: `addMenuItem('备份管理', openBackupPanel);`

- [ ] **Step 4: 验证**

Run: `cd /root/webpty && node --check public/app.js && systemctl restart webpty.service && curl -s -X POST http://127.0.0.1:4790/api/backup/create`
Expected: 201 + `{"backup":{"sha256":"..."}}`

- [ ] **Step 5: 提交**

```bash
git add public/index.html public/styles.css public/app.js
git commit -m "feat(ext): backup management panel (create/list/restore)"
```

---

### Task 14: migrator.py 导出/导入/合并 + WorkerInterface

**Files:**
- Create: `src/migrator.py`
- Test: `test/test_migrator.py`

**Interfaces:**
- Consumes: `collect_state`（Task 11 逻辑）、`Database`、config
- Produces:
  - `class WorkerInterface:` — 抽象基类：`export_state(self) -> dict` / `import_state(self, state: dict, mode: str) -> dict`（`raise NotImplementedError`）；集群预留
  - `class Migrator(WorkerInterface):` — `__init__(self, data_dir: str, config: dict, db)`; `async export(self) -> str`（生成 `data/backups/webpty-migrate-<ts>.tar.gz`，内含 `manifest.json`（含 `schema_version/source_node_id/content`）与 `state.json`，返回路径）; `async import_package(self, path: str, mode: str = "merge") -> dict`（`merge`（默认）：保留现有配置+新增；`replace`：覆盖；`dry-run`：仅预览，不写盘。防目录穿越：只读 tar 内固定名 `manifest.json`/`state.json`，不提取任意路径）; `async clone(self, template_path: str) -> dict`（= import_package(template, "merge") 的别名）; `def source_node_id(self) -> str`（读 `data_dir/node_id`，无则生成 UUID 并写入）

- [ ] **Step 1: 写失败测试**

`test/test_migrator.py`:

```python
import asyncio
import json
import os
import tempfile
import unittest

from db import Database
from migrator import Migrator, WorkerInterface


class MigratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-mig-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"port": 4790, "roots": ["/a"]}, f)
        self.db = Database(os.path.join(self.data, "webpty.db"))
        self.db.connect()
        self.config = {"port": 4790, "roots": ["/a"]}
        self.m = Migrator(self.data, self.config, self.db)

    def tearDown(self):
        self.db.close()

    async def test_worker_interface_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            WorkerInterface().export_state()

    async def test_export_creates_package(self):
        path = await self.m.export()
        self.assertTrue(os.path.exists(path))
        self.assertIn("webpty-migrate-", os.path.basename(path))
        self.assertGreater(os.path.getsize(path), 0)

    async def test_import_merge_preserves_existing(self):
        path = await self.m.export()
        # 修改现有配置制造冲突
        self.config["port"] = 9999
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["mode"], "merge")
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.config["port"], 4790)  # 包内值覆盖
        self.assertEqual(self.config["extra_key"], "mine")  # 现有键保留

    async def test_import_dry_run_does_not_write(self):
        path = await self.m.export()
        before = dict(self.config)
        res = await self.m.import_package(path, "dry-run")
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(self.config, before)

    async def test_import_replace_overwrites(self):
        path = await self.m.export()
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "replace")
        self.assertNotIn("extra_key", self.config)

    async def test_source_node_id_stable(self):
        a = self.m.source_node_id()
        b = self.m.source_node_id()
        self.assertEqual(a, b)
        self.assertTrue(len(a) > 8)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`src/migrator.py`（完整）:

```python
"""One-click config migration & environment clone (single-node; cluster
preview via WorkerInterface). Business-management layer.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
import uuid


class WorkerInterface:
    """Cluster reservation: a controller aggregates export_state() from each
    executor; single-node deployments implement this directly."""

    def export_state(self) -> dict:
        raise NotImplementedError

    def import_state(self, state: dict, mode: str) -> dict:
        raise NotImplementedError


class Migrator(WorkerInterface):
    def __init__(self, data_dir: str, config: dict, db) -> None:
        self.data_dir = data_dir
        self.config = config
        self.db = db

    def source_node_id(self) -> str:
        path = os.path.join(self.data_dir, "node_id")
        try:
            with open(path, encoding="utf-8") as f:
                nid = f.read().strip()
            if nid:
                return nid
        except OSError:
            pass
        nid = uuid.uuid4().hex[:16]
        with open(path, "w", encoding="utf-8") as f:
            f.write(nid)
        return nid

    async def export(self) -> str:
        from backup import collect_state
        state = await collect_state(self.data_dir, self.config, self.db)
        manifest = {
            "schema_version": 1,
            "created_at": time.time(),
            "source_node_id": self.source_node_id(),
            "content": ["config", "notify_rules", "sessions", "prices"],
        }
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, obj in (("manifest.json", manifest),
                              ("state.json", state)):
                data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        backups_dir = os.path.join(self.data_dir, "backups")
        os.makedirs(backups_dir, exist_ok=True)
        path = os.path.join(
            backups_dir, f"webpty-migrate-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        return path

    def _read_package(self, path: str) -> dict | None:
        """Read manifest.json + state.json from a package. Path-traversal safe:
        only fixed member names are read, nothing is extracted to disk."""
        try:
            with tarfile.open(path, "r:gz") as tf:
                man_raw = tf.extractfile("manifest.json")
                st_raw = tf.extractfile("state.json")
                if man_raw is None or st_raw is None:
                    return None
                manifest = json.loads(man_raw.read())
                state = json.loads(st_raw.read())
        except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError):
            return None
        return {"manifest": manifest, "state": state}

    async def import_package(self, path: str, mode: str = "merge") -> dict:
        pkg = self._read_package(path)
        if not pkg:
            return {"status": "error", "message": "invalid package",
                    "mode": mode}
        state = pkg["state"]
        incoming = state.get("config") or {}
        if mode == "dry-run":
            current = dict(self.config)
            changed = {k: {"current": current.get(k), "incoming": v}
                       for k, v in incoming.items()
                       if current.get(k) != v}
            return {"status": "dry-run", "mode": mode, "changes": changed}
        if mode == "replace":
            self.config.clear()
            self.config.update(incoming)
        else:  # merge (default)
            for k, v in incoming.items():
                self.config.setdefault(k, v)
        with open(os.path.join(self.data_dir, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
        await self.db.add_migration({
            "filename": os.path.basename(path),
            "source_node": pkg["manifest"].get("source_node_id"),
            "mode": mode, "status": "done",
            "log": json.dumps({"schema_version":
                               pkg["manifest"].get("schema_version")})})
        return {"status": "done", "mode": mode}

    async def clone(self, template_path: str) -> dict:
        return await self.import_package(template_path, "merge")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_migrator -v`
Expected: 6 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/migrator.py test/test_migrator.py
git commit -m "feat(ext): migrator with merge/replace/dry-run + WorkerInterface"
```

---

### Task 15: 迁移 API

**Files:**
- Modify: `src/server.py`
- Test: `test/test_migrate_api.py`

**Interfaces:**
- Consumes: Task 14 `Migrator`
- Produces: 端点：
  - `POST /api/migrate/export` → 201 `{"path": "/abs/path.tar.gz", "filename": "..."}`
  - `POST /api/migrate/import` — multipart 上传（字段 `file`，≤50MB，`mode` 可选 `merge|replace|dry-run`，默认 merge）→ 200 `{"status","mode","changes"?,"message"?}`；上传文件存 `data/uploads/` 临时文件后导入
  - `POST /api/migrate/clone`（body `{"template": "/abs/path.tar.gz"}`）→ 200 clone 结果
  - `GET /api/migrate/list` → `{"migrations": [...]}`
  - `GET /api/migrate/download/{filename}` → 200 文件流（仅允许 `data/backups/` 目录内的文件，`os.path.basename` + realpath 前缀校验防穿越；Content-Type `application/gzip`，Content-Disposition attachment）

- [ ] **Step 1: 写失败测试**

`test/test_migrate_api.py`（起服样板同前）:

```python
    def test_export(self):
        st, out = self._req("POST", "/api/migrate/export")
        self.assertEqual(st, 201)
        self.assertTrue(out["path"].endswith(".tar.gz"))

    def test_clone_missing_template(self):
        st, out = self._req("POST", "/api/migrate/clone",
                            {"template": "/nonexistent/x.tar.gz"})
        self.assertEqual(st, 200)
        self.assertEqual(out["status"], "error")

    def test_list(self):
        st, out = self._req("GET", "/api/migrate/list")
        self.assertEqual(st, 200)
        self.assertIn("migrations", out)
```

（multipart 上传的 import 端点集成测试放到 M6 手工验证——单测覆盖 export/clone/list；import 的解析函数单独单测。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /root/webpty && python3 -m unittest test.test_migrate_api -v`
Expected: FAIL — 404

- [ ] **Step 3: 实现**

server.py 加 `self.migrator = Migrator(data_dir, config, db)`（main 传入或 `__init__` 构造）。端点:

```python
        # --- migrate --------------------------------------------------------
        if path == "/api/migrate/export" and method == "POST":
            p = await self.migrator.export()
            return await self._send_json(writer, 201, {
                "path": p, "filename": os.path.basename(p)}, headers)
        if path == "/api/migrate/list" and method == "GET":
            return await self._send_json(
                writer, 200, {"migrations": await self.db.list_migrations()},
                headers)
        if path == "/api/migrate/clone" and method == "POST":
            body = await self._read_json(reader, headers)
            res = await self.migrator.clone(body.get("template", ""))
            return await self._send_json(writer, 200, res, headers)
        if path == "/api/migrate/import" and method == "POST":
            res = await self._handle_migrate_import(reader, headers)
            return await self._send_json(writer, 200, res, headers)
        m = re.match(r"^/api/migrate/download/([^/]+)$", path)
        if m and method == "GET":
            return await self._handle_migrate_download(writer, m.group(1))
```

`_handle_migrate_download`（防穿越下载）:

```python
    async def _handle_migrate_download(self, writer, filename: str):
        backups_dir = os.path.join(data_dir, "backups")
        safe = os.path.basename(filename)
        path = os.path.realpath(os.path.join(backups_dir, safe))
        if not path.startswith(os.path.realpath(backups_dir) + os.sep) \
                or not os.path.isfile(path):
            return await self._send_json(
                writer, 404, {"error": "not found"}, {})
        try:
            size = os.path.getsize(path)
            body = await asyncio.get_running_loop().run_in_executor(
                None, lambda: open(path, "rb").read())
        except OSError:
            return await self._send_json(
                writer, 404, {"error": "not found"}, {})
        headers = {"content-type": "application/gzip",
                   "content-disposition": f'attachment; filename="{safe}"',
                   "content-length": str(size)}
        writer.write(b"HTTP/1.1 200 OK\r\n" +
                     b"\r\n".join(f"{k}: {v}".encode() for k, v in headers.items()) +
                     b"\r\n\r\n" + body)
        await writer.drain()
        return True
```

`_handle_migrate_import`（解析 multipart 单文件 `file` + 可选 `mode` 字段）:

```python
    async def _handle_migrate_import(self, reader, headers) -> dict:
        length = 0
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 50 * 1024 * 1024:
            return {"status": "error", "message": "payload too large or empty"}
        body = await reader.readexactly(length)
        ct = headers.get("content-type", "")
        boundary = None
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
        if not boundary:
            return {"status": "error", "message": "missing boundary"}
        delim = b"--" + boundary.encode()
        segs = body.split(delim)
        filename = None
        file_bytes = b""
        mode = "merge"
        for seg in segs:
            if b"\r\n\r\n" not in seg:
                continue
            head, _, content = seg.partition(b"\r\n\r\n")
            head_str = head.decode("utf-8", "replace")
            content = content.rstrip(b"\r\n")
            if 'name="mode"' in head_str:
                mode = content.decode("utf-8", "replace").strip() or "merge"
            if 'name="file"' in head_str:
                for line in head_str.split("\r\n"):
                    if line.lower().startswith("content-disposition:"):
                        for bit in line.split(";"):
                            bit = bit.strip()
                            if bit.startswith("filename="):
                                filename = bit[len("filename="):].strip('"')
                file_bytes = content
        if not filename or not file_bytes:
            return {"status": "error", "message": "file field missing"}
        uploads = os.path.join(data_dir, "uploads")
        os.makedirs(uploads, exist_ok=True)
        dest = os.path.join(uploads, os.path.basename(filename))
        with open(dest, "wb") as f:
            f.write(file_bytes)
        if mode not in ("merge", "replace", "dry-run"):
            mode = "merge"
        return await self.migrator.import_package(dest, mode)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /root/webpty && python3 -m unittest test.test_migrate_api -v`
Expected: 3 tests PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add src/server.py test/test_migrate_api.py
git commit -m "feat(ext): migration export/import/clone/list REST API"
```

---

### Task 16: WebUI 迁移向导

**Files:**
- Modify: `public/index.html`、`public/styles.css`、`public/app.js`
- Test: `node --check` + curl（导出→上传→导入手工验证）

**Interfaces:**
- Consumes: Task 15 API
- Produces: `openMigratePanel()`、菜单"迁移向导"（替换 Task 5 alert 占位）；五步：导出→下载→上传→预览差异→导入

- [ ] **Step 1: HTML**

```html
  <div id="migrate-backdrop" class="panel-backdrop" hidden>
    <div class="panel">
      <div class="panel-head">
        <h2>迁移向导</h2>
        <button id="migrate-close" class="panel-close" type="button">×</button>
      </div>
      <div class="panel-body">
        <div class="notify-toolbar">
          <button id="migrate-export" type="button">1. 导出配置</button>
          <label>2. 上传包 <input id="migrate-file" type="file" accept=".tar.gz"></label>
          <select id="migrate-mode">
            <option value="merge">merge</option>
            <option value="replace">replace</option>
            <option value="dry-run">dry-run</option>
          </select>
          <button id="migrate-do" type="button">3. 导入</button>
        </div>
        <div id="migrate-result" class="notify-section"></div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: CSS** — 复用现有类，无需新增

- [ ] **Step 3: JS**

```js
const migrateBackdrop = document.getElementById('migrate-backdrop');
function openMigratePanel() { closeMenu(); migrateBackdrop.hidden = false; }
document.getElementById('migrate-close').onclick = () => { migrateBackdrop.hidden = true; };
migrateBackdrop.addEventListener('click', (ev) => {
  if (ev.target === migrateBackdrop) migrateBackdrop.hidden = true;
});
document.getElementById('migrate-export').onclick = async () => {
  const r = await api('/api/migrate/export', { method: 'POST' });
  const a = document.createElement('a');
  a.href = `/api/migrate/download/${encodeURIComponent(r.filename)}`;
  a.download = r.filename;
  a.click();
  document.getElementById('migrate-result').innerHTML = `<p>已导出 ${esc(r.filename)}（若未下载，请加服务端 download 端点）</p>`;
};
document.getElementById('migrate-do').onclick = async () => {
  const file = document.getElementById('migrate-file').files[0];
  if (!file) { alert('请先选择 .tar.gz 包'); return; }
  const mode = document.getElementById('migrate-mode').value;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  const res = await fetch('/api/migrate/import', {
    method: 'POST', body: fd,
    headers: { authorization: `Bearer ${localStorage.getItem('webpty.token') || ''}` },
  });
  const out = await res.json().catch(() => ({}));
  document.getElementById('migrate-result').innerHTML =
    `<div class="notify-item">状态: ${esc(out.status)} ${out.message ? '— ' + esc(out.message) : ''}
     ${out.changes ? '<pre>' + esc(JSON.stringify(out.changes, null, 2)) + '</pre>' : ''}</div>`;
};
```

菜单: `addMenuItem('迁移向导', openMigratePanel);`

> 注：下载端点 `GET /api/migrate/download/{filename}` 需在 Task 15 的 server.py 一并实现（仅允许 `backups/` 目录内的文件，防穿越）。

- [ ] **Step 4: 验证**

Run: `cd /root/webpty && node --check public/app.js && systemctl restart webpty.service`
然后 curl 导出一份包、再 curl 上传导入（用 `-F`）验证 merge：

```bash
curl -s -X POST http://127.0.0.1:4790/api/migrate/export
curl -s -F "file=@/root/.config/webpty/backups/webpty-migrate-*.tar.gz" -F "mode=dry-run" http://127.0.0.1:4790/api/migrate/import
```

Expected: 导出 201；导入返回 `{"status":"dry-run","changes":[...]}`

- [ ] **Step 5: 提交**

```bash
git add public/index.html public/styles.css public/app.js src/server.py
git commit -m "feat(ext): migration wizard panel + download endpoint"
```

---

### Task 17: 全量回归 + 缺陷修复

**Files:**
- 视测试结果而定（预期 0-3 个小修）
- Test: 全部

**Interfaces:**
- Consumes: 全部模块

- [ ] **Step 1: 全量测试**

Run: `cd /root/webpty && python3 -m unittest discover -s test -v 2>&1 | tail -5`
Expected: 现有 134 + 新增约 45 = ~179 tests，全部 PASS

- [ ] **Step 2: 修复任何失败**

对每个失败测试按 systematic-debugging 定位修复（多为接口签名不匹配或 import 路径），重跑直到全绿。

- [ ] **Step 3: 语法检查**

Run: `cd /root/webpty && for f in src/*.py; do python3 -m py_compile "$f" || echo "FAIL $f"; done`
Expected: 全部编译通过

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "fix(ext): regression fixes across extension modules"
```

---

### Task 18: 文档 + 部署验证 + 收尾提交

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-07-webpty-four-extensions-design.md`（状态改为"已实施"）
- Test: 部署冒烟

**Interfaces:**
- Consumes: 全部

- [ ] **Step 1: 更新 README**

在 README 功能清单加"扩展能力"小节:

```markdown
## 扩展能力（合规四大件）

- **通知中心**：会话 completed/failed/crashed/terminated 事件 → 规则匹配 → SQLite 记录 + SMTP 邮件（可选）+ WebUI 面板；60s 去重、静默时段、失败重试
- **成本账单**：agent stream-json 实时计量（claude/reasonix/opencode 等）+ 日志事后校对；内置价格表可覆盖；预算限额告警
- **备份管理**：定时（默认 24h）/手动快照 tar.gz + SHA256 校验 + 可选 AES-GCM 加密 + 轮转（默认保留 7 份）+ WebUI 恢复/差异对比
- **迁移向导**：一键导出/导入配置（merge/replace/dry-run 三种冲突策略）+ 环境克隆；WorkerInterface 集群预留

### 配置（config.json 新增段）

```json
{
  "notify": { "smtp": { "host": "", "port": 465, "tls": true,
                        "user": "", "password": "", "from": "", "to": "" } },
  "prices": { "claude": { "input": 3.0, "output": 15.0,
                          "cache_hit": 0.3, "currency": "USD" } },
  "budget": { "limit": 10.0 },
  "backup": { "retention": 7, "interval_hours": 24, "encryption_key": "" }
}
```

- `encryption_key` 留空 = 不加密（默认）；设置后且环境装有 `cryptography` 才启用 AES-GCM
- SMTP 全可选：未配置时通知仅入库 + WebUI 展示

### API 一览

`/api/notify/rules`、`/api/notify/messages`、`/api/notify/test`、
`/api/cost/summary|by-{project,tool,model,session}|alerts|budget|reconcile`、
`/api/backup/create|list|restore/{id}|diff/{a}/{b}`、
`/api/migrate/export|import|clone|list|download/{filename}`
```

- [ ] **Step 2: 更新设计文档状态**

首行 `状态：设计中（brainstorming → 待用户审阅）` 改为 `状态：已实施（2026-08-08）`

- [ ] **Step 3: 部署冒烟**

Run:
```bash
cd /root/webpty && systemctl restart webpty.service && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4790/          # 200
curl -s http://127.0.0.1:4790/api/notify/rules                           # {"rules":[]}
curl -s "http://127.0.0.1:4790/api/cost/summary?period=day"              # 零值 JSON
curl -s -X POST http://127.0.0.1:4790/api/backup/create                  # 201 + sha256
curl -s -X POST http://127.0.0.1:4790/api/migrate/export                 # 201 + path
python3 -m unittest discover -s test 2>&1 | tail -3                      # 全绿
```
Expected: 全部符合

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "docs(ext): README extension guide + design status → implemented"
git push origin main
```

- [ ] **Step 5: 里程碑验收清单**

- [ ] M1 存储层: `test_db.py` 7 用例过，WAL 生效
- [ ] M2 通知: 规则 CRUD API + 事件→记录→SMTP(mock) 全链路测试过
- [ ] M3 成本: 实时解析 + 校对补录 + 预算告警测试过
- [ ] M4 备份: 创建/恢复/轮转/差异测试过，定时 loop 部署
- [ ] M5 迁移: 导出/导入 merge/replace/dry-run 测试过
- [ ] M6: 全量测试绿、部署冒烟过、README 更新、git 推送
