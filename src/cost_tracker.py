"""Realtime token & cost tracking from agent stream-json events.

Subscribes to session_manager's `agentEvent` bus; parses usage, computes
cost via price_table, persists to token_usage (source=realtime) and keeps
a budget limit. Business-management layer.
"""
from __future__ import annotations

import asyncio
import json

from logging_util import log_error
from price_table import cost_for
from usage_parser import parse_usage


class CostTracker:
    def __init__(self, db, config: dict) -> None:
        self.db = db
        self.config = config
        self._budget: float = float(
            (config.get("budget") or {}).get("limit", 0.0) or 0.0)
        self._tasks: set[asyncio.Task] = set()

    def handle_agent_event(self, event: dict, sid: str | None = None) -> None:
        task = asyncio.create_task(self._record(event, sid))
        self._tasks.add(task)
        task.add_done_callback(self._on_record_done)

    def _on_record_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        err = task.exception()
        if err is not None:
            log_error("cost_tracker", err)

    async def _record(self, event: dict, sid: str | None = None) -> None:
        usage = None
        u = event.get("usage")
        if isinstance(u, dict) and u:
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
        session_id = usage.get("session_id") or sid or event.get("session_id")
        # Dedup against posthoc rows: if the reconciler already recorded this
        # exact (session, tokens) pair, skip to avoid double billing.
        dup = await self.db.query_one(
            "SELECT 1 AS x FROM token_usage WHERE session_id=? AND "
            "tokens_in=? AND tokens_out=? AND source='posthoc' LIMIT 1",
            (session_id or "", usage["tokens_in"], usage["tokens_out"]))
        if dup:
            return
        await self.db.add_usage({
            "project": event.get("project"),
            "tool": event.get("tool"),
            "model": model,
            "session_id": session_id,
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
