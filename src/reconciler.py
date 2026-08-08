"""Post-hoc usage reconciliation: scan agent log dirs and backfill records
that the realtime parser missed (source=posthoc). Business-management layer.
"""
from __future__ import annotations

import os

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
