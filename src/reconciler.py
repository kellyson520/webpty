"""Post-hoc usage reconciliation: scan agent log dirs and backfill records
that the realtime parser missed (source=posthoc). Business-management layer.
"""
from __future__ import annotations

import os

from price_table import cost_for
from usage_parser import parse_usage

# Single-file cap for reconcile scans (plan Task 5): skip logs larger than
# 50MB so one runaway session can't stall the whole scan.
MAX_SCAN_FILE_BYTES = 50 * 1024 * 1024


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
            # Audit F1: files over the cap used to be skipped ENTIRELY —
            # long claude sessions' costs were never recorded. Read the
            # trailing MAX_SCAN_FILE_BYTES instead (a >50MB log still costs
            # at most one bounded pass and the most recent usage lives at
            # the tail).
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    if size > MAX_SCAN_FILE_BYTES:
                        f.seek(size - MAX_SCAN_FILE_BYTES)
                        f.readline()  # drop the partial first line
                    for line in f:
                        u = parse_usage(line, "claude")
                        if u:
                            u["session_id"] = session_id
                            u["project"] = root
                            out.append(u)
            except OSError:
                continue
    return out


def scan_tool_logs(sessions_dir: str, tool: str,
                   model_hint: str | None = None) -> list[dict]:
    """Audit H1 (v22): generic post-hoc scan for tools that persist chat
    JSONL without per-turn usage (reasonix/opencode). Tokens are ESTIMATED
    from content length (chars/4); the model comes from the filename
    (<ts>-<model>.jsonl) or the hint. Cost is computed downstream via the
    price table — these rows are estimates by nature (source=posthoc).
    """
    import json
    out: list[dict] = []
    if not os.path.isdir(sessions_dir):
        return out
    for root, _dirs, files in os.walk(sessions_dir):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            session_id = fn[:-6]
            path = os.path.join(root, fn)
            model = model_hint
            if not model:
                # reasonix filenames: <ts>-<model>.jsonl where the ts itself
                # contains dashes (20260808-013650.798429509-deepseek-v4-flash)
                # — strip the leading numeric timestamp, keep the rest.
                import re as _re
                base = fn[:-6]
                m = _re.match(r"^[\d.:\-]+-", base)
                model = base[m.end():] if m else (base or tool)
                # Audit H3 (v28): recovery/checkpoint copies embed the model
                # in the middle: deepseek-v4-flash-6b0ea59…-recovery-f56c….jsonl
                # — the tail parsed as the model name and fell back to the
                # whole-family price (0.27 vs 0.02 → 13× overcharge). Cut at
                # the longest known model prefix from the price table.
                from price_table import longest_prefix
                best = longest_prefix(model)
                if best:
                    model = model[:len(best)]
            t_in = t_out = 0
            try:
                size = os.path.getsize(path)
                with open(path, encoding="utf-8", errors="replace") as f:
                    if size > MAX_SCAN_FILE_BYTES:
                        f.seek(size - MAX_SCAN_FILE_BYTES)
                        f.readline()
                    for line in f:
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(evt, dict):
                            continue
                        role = evt.get("role")
                        content = evt.get("content") or ""
                        if isinstance(content, list):
                            content = " ".join(
                                str(c.get("text") or "") if isinstance(c, dict) else str(c)
                                for c in content)
                        est = max(1, len(str(content)) // 4)
                        if role == "user":
                            t_in += est
                        elif role == "assistant":
                            t_out += est
            except OSError:
                continue
            if t_in or t_out:
                out.append({
                    "session_id": session_id, "project": root,
                    "tokens_in": t_in, "tokens_out": t_out,
                    "model": model or tool,
                })
    return out


class Reconciler:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config

    async def reconcile(self, projects_dir: str, tool: str = "claude") -> int:
        # Audit F2: batch dedup + insert instead of one SELECT + one INSERT
        # (each with its own commit) per row — 100k rows went from minutes
        # to seconds.
        rows = list(scan_claude_logs(projects_dir))
        if not rows:
            return 0
        # Audit C1: dedup key includes model — two independent calls in the
        # same session with identical token counts (very common for short
        # queries) are distinct billable events; (session,tokens) alone
        # silently dropped the second one forever.
        keys = [(u.get("session_id") or "", u["tokens_in"], u["tokens_out"],
                 u.get("model") or tool)
                for u in rows]
        seen = set()
        # Chunk the key query to stay inside SQLite's variable limit.
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            part = keys[i:i + CHUNK]
            placeholders = ",".join(["(?,?,?,?)"] * len(part))
            flat = [x for k in part for x in k]
            existing = await self.db.query(
                f"SELECT session_id, tokens_in, tokens_out, model FROM token_usage "
                f"WHERE (session_id, tokens_in, tokens_out, model) IN ({placeholders})",
                tuple(flat))
            for r in existing:
                seen.add((r["session_id"] or "", r["tokens_in"], r["tokens_out"],
                          r["model"] or tool))
        batch = []
        for u in rows:
            model = u.get("model") or tool
            key = (u.get("session_id") or "", u["tokens_in"], u["tokens_out"],
                   model)
            if key in seen:
                continue
            cost = u.get("cost")
            if cost is None:
                cost = cost_for(model, u["tokens_in"], u["tokens_out"],
                                self.config, cached_in=u.get("cached_in", 0),
                                cached_write=u.get("cached_write", 0))
            batch.append({
                "project": u.get("project"), "tool": tool, "model": model,
                "session_id": u.get("session_id"),
                "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"],
                "cost": cost, "source": "posthoc"})
        if batch:
            await self.db.add_usage_batch(batch)
        return len(batch)

    async def _add_one(self, u: dict, tool: str) -> int:
        """Insert a single scanned usage row (dedup + cost). Returns 1 when
        inserted, 0 when it was a duplicate."""
        key = (u.get("session_id") or "", u["tokens_in"], u["tokens_out"])
        dup = await self.db.query_one(
            "SELECT 1 AS x FROM token_usage WHERE session_id=? AND "
            "tokens_in=? AND tokens_out=? LIMIT 1",
            key)
        if dup:
            return 0
        model = u.get("model") or tool
        cost = u.get("cost")
        if cost is None:
            cost = cost_for(model, u["tokens_in"], u["tokens_out"],
                            self.config, cached_in=u.get("cached_in", 0),
                            cached_write=u.get("cached_write", 0))
        await self.db.add_usage({
            "project": u.get("project"), "tool": tool, "model": model,
            "session_id": u.get("session_id"),
            "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"],
            "cost": cost, "source": "posthoc"})
        return 1
