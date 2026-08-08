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
            CREATE INDEX IF NOT EXISTS idx_usage_session
                ON token_usage (session_id, tokens_in, tokens_out);

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
