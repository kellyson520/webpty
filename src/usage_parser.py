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
    # OpenAI/codex 用 prompt_tokens/completion_tokens;Anthropic 用
    # input_tokens/output_tokens — 两者都归一。
    tokens_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    tokens_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    # 缓存读:Anthropic cache_read_input_tokens / OpenAI input_tokens_cached
    # / cached_tokens / prompt_tokens_details.cached_tokens
    cached_in = (usage.get("cache_read_input_tokens")
                 or usage.get("input_tokens_cached")
                 or usage.get("cached_tokens")
                 or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                 or 0)
    # 缓存写(按 input 价计,与缓存读分开)
    cached_write = usage.get("cache_creation_input_tokens") or 0
    return {
        "tokens_in": _to_int(tokens_in),
        "tokens_out": _to_int(tokens_out),
        "cached_in": _to_int(cached_in),
        "cached_write": _to_int(cached_write),
    }


def parse_usage(line: str, tool: str) -> dict | None:
    try:
        return _parse_usage_inner(line, tool)
    except Exception:  # noqa: BLE001 — contract: NEVER raise, return None
        return None


def _parse_usage_inner(line: str, tool: str) -> dict | None:
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
        if u["tokens_in"] or u["tokens_out"] or u["cached_in"] or u["cached_write"]:
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
            "cached_write": _to_int(obj.get("cached_write") or 0),
            "cost": obj.get("cost"),
            "model": obj.get("model"),
            "session_id": obj.get("session_id"),
        }
    return None
