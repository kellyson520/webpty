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
