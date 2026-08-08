"""Parse token usage from agent stream-json lines (realtime source).

Returns None when the line carries no usage → the reconciler picks it up
later from logs (posthoc source). Business-management layer.
"""
from __future__ import annotations

import json


def _to_int(v: object) -> int:
    """Coerce a token count to int; unparseable values become 0."""
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _extract(usage: dict) -> dict:
    return {
        "tokens_in": _to_int(usage.get("input_tokens") or 0),
        "tokens_out": _to_int(usage.get("output_tokens") or 0),
        "cached_in": _to_int(usage.get("input_tokens_cached")
                              or usage.get("cache_creation_input_tokens") or 0),
    }


def parse_usage(line: str, tool: str) -> dict | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    if isinstance(usage, dict):
        u = _extract(usage)
        if u["tokens_in"] or u["tokens_out"] or u["cached_in"]:
            msg = obj.get("message")
            model = (msg.get("model") if isinstance(msg, dict) else None) \
                or obj.get("model")
            return {
                **u,
                "cost": None,  # computed by cost_tracker via price_table
                "model": model,
                "session_id": obj.get("session_id"),
            }
    if obj.get("type") in ("stats", "usage_event") and (
            obj.get("tokens_in") is not None or obj.get("tokens_out") is not None):
        return {
            "tokens_in": _to_int(obj.get("tokens_in") or 0),
            "tokens_out": _to_int(obj.get("tokens_out") or 0),
            "cached_in": _to_int(obj.get("cached_in") or 0),
            "cost": obj.get("cost"),
            "model": obj.get("model"),
            "session_id": obj.get("session_id"),
        }
    return None
