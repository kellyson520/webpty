"""Model price table (per 1M tokens) with built-in defaults + config override.

Lookup order (first match wins):
  1. exact key in config["prices"]  (user override)
  2. exact key in DEFAULT_PRICES
  3. longest prefix in PREFIX_PRICES (e.g. "claude-opus-4-8" → claude-opus)
  4. tool family prefix (e.g. "gpt-5.4" → openai)
  5. _FALLBACK
"""
from __future__ import annotations

DEFAULT_PRICES: dict[str, dict] = {
    # Anthropic Claude (per 1M tokens, USD)
    "claude-opus":   {"input": 15.0, "output": 75.0, "cache_hit": 1.5,  "currency": "USD"},
    "claude-sonnet": {"input": 3.0,  "output": 15.0, "cache_hit": 0.3,  "currency": "USD"},
    "claude-haiku":  {"input": 0.80, "output": 4.0,  "cache_hit": 0.08, "currency": "USD"},
    "claude":        {"input": 3.0,  "output": 15.0, "cache_hit": 0.3,  "currency": "USD"},
    # OpenAI / Codex
    "gpt-5":         {"input": 1.25, "output": 10.0, "cache_hit": 0.125, "currency": "USD"},
    "gpt-4":         {"input": 2.5,  "output": 10.0, "cache_hit": 0.5,   "currency": "USD"},
    "gpt-4o":        {"input": 2.5,  "output": 10.0, "cache_hit": 0.5,   "currency": "USD"},
    "gpt-4.1":       {"input": 2.0,  "output": 8.0,  "cache_hit": 0.5,   "currency": "USD"},
    "o1":            {"input": 15.0, "output": 60.0, "cache_hit": 1.5,   "currency": "USD"},
    "o3":            {"input": 2.0,  "output": 8.0,  "cache_hit": 0.5,   "currency": "USD"},
    "codex":         {"input": 2.5,  "output": 10.0, "cache_hit": 0.5,   "currency": "USD"},
    # Audit E1: exact-version entries beat the family prefix (longest
    # prefix wins), so these correct the family price where it was wrong.
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_hit": 0.10, "currency": "USD"},
    "gpt-4o-mini":      {"input": 0.15, "output": 0.60, "cache_hit": 0.075, "currency": "USD"},
    "gpt-5-mini":       {"input": 0.25, "output": 2.0, "cache_hit": 0.025, "currency": "USD"},
    "gpt-5-nano":       {"input": 0.05, "output": 0.40, "cache_hit": 0.005, "currency": "USD"},
    "codex-mini":       {"input": 0.25, "output": 2.0, "cache_hit": 0.025, "currency": "USD"},
    "deepseek-chat":    {"input": 0.28, "output": 0.42, "cache_hit": 0.028, "currency": "USD"},
    # Reasonix / OpenCode (DeepSeek-class gateway pricing)
    "reasonix":      {"input": 0.55, "output": 2.19, "cache_hit": 0.07,  "currency": "USD"},
    "opencode":      {"input": 0.55, "output": 2.19, "cache_hit": 0.07,  "currency": "USD"},
    # DeepSeek
    "deepseek":      {"input": 0.27, "output": 1.1,  "cache_hit": 0.07,  "currency": "USD"},
    "deepseek-v4":   {"input": 0.27, "output": 1.1,  "cache_hit": 0.07,  "currency": "USD"},
    "deepseek-v3":   {"input": 0.27, "output": 1.1,  "cache_hit": 0.07,  "currency": "USD"},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_hit": 0.07, "currency": "USD"},
    # Codex gateway model (codex CLI reports these)
    "o3-mini":       {"input": 1.1,  "output": 4.4,  "cache_hit": 0.55,  "currency": "USD"},
    "o4-mini":       {"input": 1.1,  "output": 4.4,  "cache_hit": 0.55,  "currency": "USD"},
}
_FALLBACK = {"input": 1.0, "output": 2.0, "cache_hit": 0.1, "currency": "USD"}


def _match_default(model: str) -> dict | None:
    """Exact match, then longest prefix, then family prefix."""
    if model in DEFAULT_PRICES:
        return DEFAULT_PRICES[model]
    # Longest prefix that names a real model family, e.g.
    # "claude-opus-4-8" → "claude-opus", "gpt-5.4-mini" → "gpt-5".
    best = None
    for key in DEFAULT_PRICES:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    if best:
        return DEFAULT_PRICES[best]
    # Tool-family fallbacks for models we don't list explicitly.
    lower = model.lower()
    if lower.startswith("claude"):
        return DEFAULT_PRICES["claude"]
    if lower.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return DEFAULT_PRICES["gpt-5"]
    if lower.startswith(("deepseek", "ds-")):
        return DEFAULT_PRICES["deepseek"]
    if lower.startswith("gemini"):
        return {"input": 0.15, "output": 0.60, "cache_hit": 0.015, "currency": "USD"}
    return None


def get_price(model: str, config: dict) -> dict:
    prices = config.get("prices") or {}
    if isinstance(prices, dict):
        # Exact match first, then longest-prefix match (audit E2: a
        # configured "claude-haiku-4-5" must cover the dated
        # "claude-haiku-4-5-20251001" id too).
        if model in prices and isinstance(prices[model], dict):
            return prices[model]
        best = None
        for key in prices:
            if (model.startswith(key) and isinstance(prices[key], dict)
                    and (best is None or len(key) > len(best))):
                best = key
        if best is not None:
            return prices[best]
    hit = _match_default(model)
    return hit if hit is not None else _FALLBACK


def cost_for(model: str, tokens_in: int, tokens_out: int, config: dict,
             cached_in: int = 0, cached_write: int = 0) -> float:
    p = get_price(model, config)
    fresh = max(tokens_in - max(cached_in, 0) - max(cached_write, 0), 0)
    total = (fresh * float(p.get("input", 1.0))
             + max(cached_in, 0) * float(p.get("cache_hit", 0.1))
             + max(cached_write, 0) * float(p.get("input", 1.0))
             + tokens_out * float(p.get("output", 2.0))) / 1_000_000.0
    return max(total, 0.0)
