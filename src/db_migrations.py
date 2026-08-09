# src/db_migrations.py
"""Ordered SQLite schema migrations (audit L6).

Each entry is (version, sql) run in order against any DB whose
PRAGMA user_version is below `version`. Keep this list append-only —
never edit an applied migration, only add new ones at the end.

Current schema is at version 1 (baseline captured from _create_schema;
no structural change yet — future column additions go here instead of
ad-hoc lazy ALTERs).
"""

MIGRATIONS: list[tuple[int, str]] = [
    # v1: legacy DBs predate the dedup_key column — the ALTER itself is
    # applied in db.py (guarded by a column-existence check); this SQL adds
    # the indexes that depend on it.
    (1, """
        CREATE INDEX IF NOT EXISTS idx_notif_dedup
            ON notifications (dedup_key, ts);
        CREATE INDEX IF NOT EXISTS idx_notif_ts
            ON notifications (ts);
        CREATE INDEX IF NOT EXISTS idx_usage_src
            ON token_usage (session_id, source);
    """),
]
