# src/logging_util.py
"""Shared error-visible logging for webpty — keeps stack traces minimal while
making swallowed exceptions observable via a tagged stdout line AND a
bounded ring buffer exposed as GET /api/errors (audit S2: backend failures
were invisible to the UI)."""
import time
from collections import deque

_ERRORS: deque = deque(maxlen=200)


def log_error(tag: str, err: Exception) -> None:
    print(f"[webpty:{tag}] {type(err).__name__}: {err}", flush=True)
    _ERRORS.append({
        "ts": time.time(),
        "tag": tag,
        "type": type(err).__name__,
        "message": str(err)[:500],
    })


def recent_errors(limit: int = 50) -> list[dict]:
    return list(_ERRORS)[-limit:]
