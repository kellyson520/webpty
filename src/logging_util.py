# src/logging_util.py
"""Shared error-visible logging for webpty — keeps stack traces minimal while
making swallowed exceptions observable via a tagged stdout line."""
import time


def log_error(tag: str, err: Exception) -> None:
    print(f"[webpty:{tag}] {type(err).__name__}: {err}", flush=True)
