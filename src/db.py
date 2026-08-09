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
        # Audit M3: the DB stores notifications/usage — owner-only perms.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
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
            -- audit M3: the estimated-cost NOT EXISTS subquery filters by
            -- (session_id, source) — without source in the index every
            -- summary call scans all rows per session.
            CREATE INDEX IF NOT EXISTS idx_usage_src
                ON token_usage (session_id, source);

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
        # Audit N2: matched_rules column for notification auditability —
        # added lazily so existing DBs migrate without a version bump.
        for col, ddl in (("matched_rules", "TEXT"),
                         ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                         ("last_error", "TEXT"),
                         # Audit M5 (v24): read state for the notification
                         # center (unread badge / mark-all-read).
                         ("read", "INTEGER NOT NULL DEFAULT 0")):
            try:
                self._conn.execute(
                    f"ALTER TABLE notifications ADD COLUMN {col} {ddl}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Audit L6: schema versioning — ordered migrations run once each
        # against any DB regardless of how old it is.
        from db_migrations import MIGRATIONS
        try:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
        except (sqlite3.OperationalError, TypeError, IndexError):
            ver = 0
        for v, sql in MIGRATIONS:
            if v > ver:
                try:
                    if v == 1:
                        # dedup_key exists on fresh DBs (built by CREATE);
                        # only legacy DBs need the ALTER.
                        cols = {r[1] for r in self._conn.execute(
                            "PRAGMA table_info(notifications)")}
                        if "dedup_key" not in cols:
                            self._conn.execute(
                                "ALTER TABLE notifications ADD COLUMN "
                                "dedup_key TEXT NOT NULL DEFAULT ''")
                    self._conn.executescript(sql)
                    self._conn.execute(f"PRAGMA user_version = {v}")
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass  # already applied manually / partial env
        # dedup_key-dependent indexes — built AFTER migrations so legacy
        # DBs (column added by v1) can create them too.
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notif_dedup "
                "ON notifications (dedup_key, ts)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notif_ts "
                "ON notifications (ts)")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

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

    async def prune_old_data(self, retention_days: int = 90) -> dict:
        """Delete notifications and usage rows older than retention_days,
        then checkpoint the WAL (low-footprint: bounded DB growth)."""
        cutoff = _ts() - retention_days * 86400
        # Audit L6: cap the notifications table absolutely — keep the most
        # recent 500 rows even if the 90-day window grows large.
        deleted_notif = await self.execute(
            "DELETE FROM notifications WHERE ts < ? OR id NOT IN "
            "(SELECT id FROM notifications ORDER BY id DESC LIMIT 500)",
            (cutoff,))
        deleted_usage = await self.execute(
            "DELETE FROM token_usage WHERE ts < ?", (cutoff,))
        # WAL checkpoint: shrink the -wal file so it can't grow unbounded.
        try:
            assert self._conn is not None
            async with self._lock:
                row = self._conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            checkpoint = dict(row) if row else {}
        except Exception:  # noqa: BLE001 — checkpoint is best-effort
            checkpoint = {}
        return {"deleted_notifications": deleted_notif,
                "deleted_usage": deleted_usage,
                "checkpoint": checkpoint}

    # ---- notifications -------------------------------------------------
    async def add_notification(self, n: dict) -> int:
        return await self.execute(
            """INSERT INTO notifications
               (ts, event_type, level, tool, project, session_id, title, body,
                dedup_key, delivered, matched_rules)
               VALUES (?,?,?,?,?,?,?,?,?,0,?)""",
            (n.get("ts", _ts()), n["event_type"], n.get("level", "info"),
             n.get("tool"), n.get("project"), n.get("session_id"),
             n["title"], n.get("body"), n["dedup_key"],
             n.get("matched_rules")))

    async def list_notifications(self, page: int, page_size: int = 20) -> dict:
        total = await self.query_one(
            "SELECT COUNT(*) AS c FROM notifications")
        unread = await self.query_one(
            "SELECT COUNT(*) AS c FROM notifications WHERE read=0")
        rows = await self.query(
            "SELECT * FROM notifications ORDER BY ts DESC LIMIT ? OFFSET ?",
            (page_size, (max(page, 1) - 1) * page_size))
        return {"total": total["c"] if total else 0,
                "unread": unread["c"] if unread else 0,
                "items": rows}

    async def mark_read(self, notif_id: int) -> None:
        """Audit M5 (v24): notification read state."""
        await self.execute("UPDATE notifications SET read=1 WHERE id=?", (notif_id,))

    async def mark_all_read(self) -> int:
        """Audit M5 (v24): batch read; returns the number updated.
        Audit M3 (v25): bound by the CURRENT max id — a notification that
        arrives mid-batch must not be marked read."""
        row = await self.query_one(
            "SELECT MAX(id) AS m FROM notifications")
        max_id = row["m"] if row and row["m"] is not None else 0
        cur = await self.execute(
            "UPDATE notifications SET read=1 WHERE read=0 AND id <= ?",
            (max_id,))
        return cur if cur else 0

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
        # Audit T4: only rows still under the retry cap (attempts < 10).
        return await self.query(
            "SELECT * FROM notifications WHERE delivered=0 AND attempts < 10 "
            "ORDER BY ts ASC LIMIT ?",
            (limit,))

    async def bump_notify_attempt(self, notif_id: int, err: str) -> None:
        """Audit T4: record a failed send attempt; mark dead at the cap."""
        await self.execute(
            "UPDATE notifications SET attempts = attempts + 1, last_error = ? "
            "WHERE id = ?", (err, notif_id))
        await self.execute(
            "UPDATE notifications SET delivered = 2 WHERE id = ? AND attempts >= 10",
            (notif_id,))

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

    async def add_usage_batch(self, rows: list[dict]) -> int:
        """Bulk insert in one transaction (audit F2): the reconciler pushes
        thousands of posthoc rows at once — one commit per row was the
        bottleneck."""
        if not rows:
            return 0
        assert self._conn is not None
        async with self._lock:
            self._conn.executemany(
                """INSERT INTO token_usage
                   (ts, project, tool, model, session_id, tokens_in,
                    tokens_out, cost, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(u.get("ts", _ts()), u.get("project"), u.get("tool"),
                  u.get("model"), u.get("session_id"),
                  int(u.get("tokens_in", 0)), int(u.get("tokens_out", 0)),
                  float(u.get("cost", 0.0)), u.get("source", "posthoc"))
                 for u in rows])
            self._conn.commit()
        return len(rows)

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
                      COALESCE(SUM(CASE WHEN source='actual'
                                        THEN cost ELSE 0 END),0) AS cost,
                      COALESCE(SUM(CASE WHEN source!='actual'
                              AND NOT EXISTS (SELECT 1 FROM token_usage t2
                                   WHERE t2.session_id = token_usage.session_id
                                     AND t2.source='actual')
                                THEN cost ELSE 0 END),0) AS estimated,
                      COUNT(*) AS entries
               FROM token_usage WHERE ts>=?""",
            (self._period_start(period),))
        return dict(row) if row else {"tokens_in": 0, "tokens_out": 0,
                                      "cost": 0.0, "estimated": 0.0,
                                      "entries": 0}

    async def usage_grouped(self, group: str, period: str) -> list[dict]:
        col = {"project": "project", "tool": "tool", "model": "model",
               "session_id": "session_id"}.get(group, "project")
        return await self.query(
            f"""SELECT {col} AS name,
                       COALESCE(SUM(tokens_in),0) AS tokens_in,
                       COALESCE(SUM(tokens_out),0) AS tokens_out,
                       COALESCE(SUM(CASE WHEN source='actual'
                                         THEN cost ELSE 0 END),0) AS cost,
                       COALESCE(SUM(CASE WHEN source!='actual'
                               AND NOT EXISTS (SELECT 1 FROM token_usage t2
                                    WHERE t2.session_id = token_usage.session_id
                                      AND t2.source='actual')
                                 THEN cost ELSE 0 END),0) AS estimated
                FROM token_usage WHERE ts>=? GROUP BY {col}
                -- Audit M2 (v24): estimate-only models (reasonix: no usage
                -- report) had cost=0 and sorted to the BOTTOM with $0.00 —
                -- rank by cost+estimated.
                ORDER BY (cost + estimated) DESC""",
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

    async def delete_backup_by_filename(self, filename: str) -> None:
        await self.execute("DELETE FROM backups WHERE filename=?",
                           (filename,))

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
