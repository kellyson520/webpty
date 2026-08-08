"""Notification rule model and matcher (business-management layer).

Rules match session_event payloads; quiet hours suppress delivery windows.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

EVENT_TYPES = ("completed", "failed", "crashed", "terminated")


def _matcher(rule: dict) -> dict | None:
    """Parsed matcher, or None when matcher_json is invalid (no match)."""
    try:
        m = json.loads(rule.get("matcher_json") or "{}")
        return m if isinstance(m, dict) else {}
    except json.JSONDecodeError:
        return None


def match_rule(rule: dict, event: dict) -> bool:
    """True when event satisfies rule's event_type and matcher subset."""
    if rule.get("event_type") and rule.get("event_type") != event.get("type"):
        return False
    m = _matcher(rule)
    if m is None:
        return False
    for key, want in m.items():
        got = event.get(key)
        if want is not None and got != want:
            return False
    return True


def quiet_hours_active(rule: dict, now: datetime | None = None) -> bool:
    """True when `now` falls inside the rule's quiet window ('' = no limit)."""
    start = rule.get("quiet_start") or ""
    end = rule.get("quiet_end") or ""
    if not start or not end:
        return False
    now = now or datetime.now()
    try:
        sh, sm = (int(x) for x in start.split(":", 1))
        eh, em = (int(x) for x in end.split(":", 1))
    except ValueError:
        return False
    cur = now.hour * 60 + now.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s <= e:  # same-day window
        return s <= cur < e
    return cur >= s or cur < e  # wraps past midnight
