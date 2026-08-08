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
        # Per-session last cumulative usage (codex/reasonix send cumulative
        # values; only the delta is billed — see _record).
        self._last_usage: dict[str, dict] = {}

    def on_session_event(self, event: dict) -> None:
        """Clear per-session cumulative state when the session ends."""
        if event.get("type") in ("completed", "failed", "crashed",
                                 "terminated"):
            self._last_usage.pop(event.get("session_id"), None)

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
            # 事件自带 usage dict(codex prompt_tokens / Anthropic input_tokens)
            # — 用 _extract 归一化(不期望整行 JSON)。
            from usage_parser import _extract
            try:
                norm = _extract(u)
                usage = {
                    **norm,
                    "cost": u.get("cost"),
                    "model": u.get("model"),
                    "session_id": event.get("session_id"),
                }
            except Exception:  # noqa: BLE001
                usage = None
        else:
            raw = event.get("raw")
            if isinstance(raw, str):
                usage = parse_usage(raw, event.get("tool") or "")
            elif isinstance(raw, dict):
                usage = parse_usage(json.dumps(raw), event.get("tool") or "")
        if not usage:
            return
        model = usage.get("model") or event.get("tool") or "unknown"
        session_id = usage.get("session_id") or sid or event.get("session_id")
        # Cumulative-delta billing: codex/reasonix report per-event usage as
        # running totals for the conversation. Only the increase over the
        # last recorded event is billed; equal values are idempotent (skipped).
        last = self._last_usage.get(session_id)
        if last:
            delta_in = max(usage["tokens_in"] - last["tokens_in"], 0)
            delta_out = max(usage["tokens_out"] - last["tokens_out"], 0)
            delta_cached = max(usage.get("cached_in", 0)
                               - last.get("cached_in", 0), 0)
            delta_cwrite = max(usage.get("cached_write", 0)
                               - last.get("cached_write", 0), 0)
            if delta_in == 0 and delta_out == 0 and delta_cached == 0 \
                    and delta_cwrite == 0:
                return  # idempotent repeat — nothing new to bill
        else:
            delta_in, delta_out = usage["tokens_in"], usage["tokens_out"]
            delta_cached = usage.get("cached_in", 0)
            delta_cwrite = usage.get("cached_write", 0)
        self._last_usage[session_id] = {
            "tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"],
            "cached_in": usage.get("cached_in", 0),
            "cached_write": usage.get("cached_write", 0),
        }
        # Cost is recomputed on the DELTA (usage.get("cost") would be the
        # cumulative total — double billing).
        cost = cost_for(model, delta_in, delta_out, self.config,
                        cached_in=delta_cached, cached_write=delta_cwrite)
        # Dedup against posthoc rows: if the reconciler already recorded this
        # exact (session, tokens) pair, skip to avoid double billing.
        dup = await self.db.query_one(
            "SELECT 1 AS x FROM token_usage WHERE session_id=? AND "
            "tokens_in=? AND tokens_out=? AND source='posthoc' LIMIT 1",
            (session_id or "", delta_in, delta_out))
        if dup:
            return
        await self.db.add_usage({
            "project": event.get("project"),
            "tool": event.get("tool"),
            "model": model,
            "session_id": session_id,
            "tokens_in": delta_in, "tokens_out": delta_out,
            "cached_in": delta_cached, "cached_write": delta_cwrite,
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
