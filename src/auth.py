"""Identity gate for webpty.

1. If `authToken` is set, every non-localhost request must present it
   (Authorization: Bearer <token>, ?token=<token>, or the `webpty_token`
   cookie). This is the lightweight self-hosted gate — no Tailscale required.
2. Otherwise we fall back to the Tailscale identity gate: shell out to
   `tailscale whois --json` to map a peer IP back to its tailnet login;
   requests are allowed when the login is in `allowedLogins`. With an empty
   `allowedLogins` the gate is disabled (legacy behavior) — the boot log
   nudges the operator.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import subprocess
import time
import urllib.parse

_CACHE_TTL_S = 60.0
_NEG_CACHE_TTL_S = 10.0
_whois_cache: dict[str, tuple[dict | None, float]] = {}
_tailscale_missing_logged = False


def extract_token(headers: dict, url: str) -> str:
    """Pull a token from Authorization header, ?token= query, or cookie."""
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    query = urllib.parse.urlparse(url or "/").query
    params = urllib.parse.parse_qs(query)
    if "token" in params and params["token"]:
        return params["token"][0]

    cookie = headers.get("cookie") or headers.get("Cookie")
    if isinstance(cookie, str):
        for part in cookie.split(";"):
            part = part.strip()
            if part.lower().startswith("webpty_token="):
                raw = part.split("=", 1)[1]
                # urllib.unquote is lenient with malformed percent-encoding;
                # match JS decodeURIComponent by rejecting invalid sequences
                # (% not followed by two hex digits) — a clean 403 beats a 500.
                if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                    return ""
                try:
                    return urllib.parse.unquote(raw)
                except (ValueError, UnicodeDecodeError):
                    return ""
    return ""


def token_matches(token: str, req_headers: dict, url: str) -> bool:
    if not token:
        return False
    candidate = extract_token(req_headers, url)
    return hmac.compare_digest(token, candidate)


def normalize_peer_ip(addr: str | None) -> str:
    if not addr:
        return ""
    # IPv4-mapped IPv6 (`::ffff:127.0.0.1`) → plain v4
    if addr.lower().startswith("::ffff:"):
        return addr[7:]
    return addr


def is_localhost(ip: str) -> bool:
    n = normalize_peer_ip(ip)
    return n == "127.0.0.1" or n == "::1" or n.startswith("127.")


def _tailscale_whois(ip: str) -> dict | None:
    cached = _whois_cache.get(ip)
    if cached and cached[1] > time.time():
        return cached[0]
    global _tailscale_missing_logged
    value: dict | None = None
    try:
        result = subprocess.run(
            ["tailscale", "whois", "--json", ip],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            import json as _json

            info = _json.loads(result.stdout)
            login_name = (info or {}).get("UserProfile", {}).get("LoginName")
            display_name = (info or {}).get("UserProfile", {}).get("DisplayName")
            node_name = (info or {}).get("Node", {}).get("Name")
            if node_name:
                node_name = node_name.rstrip(".")
            if login_name:
                value = {"loginName": login_name, "displayName": display_name, "nodeName": node_name}
    except FileNotFoundError:
        if not _tailscale_missing_logged:
            _tailscale_missing_logged = True
            print("[webpty] `tailscale` CLI not found on PATH — identity-based auth disabled")
    except (subprocess.TimeoutExpired, ValueError, KeyError):
        pass
    ttl = _CACHE_TTL_S if value else _NEG_CACHE_TTL_S
    _whois_cache[ip] = (value, time.time() + ttl)
    return value


async def authorize_peer(ip: str, req_headers: dict, url: str,
                         allowed_logins: list[str], auth_token: str = "") -> dict:
    """Returns {ok, reason, peer}. ok=False is a deliberate deny → 403."""
    ip = normalize_peer_ip(ip)
    peer = {"ip": ip}
    if is_localhost(ip):
        return {"ok": True, "reason": "localhost", "peer": {**peer, "login": "localhost"}}

    if auth_token:
        if token_matches(auth_token, req_headers, url):
            return {"ok": True, "reason": "token", "peer": peer}
        return {"ok": False, "reason": "bad-token", "peer": peer}

    if not allowed_logins:
        # Gate disabled — let everything through.
        return {"ok": True, "reason": "gate-disabled", "peer": peer}

    # Tailscale identity gate — run whois in a thread so the event loop
    # isn't blocked (subprocess.run is synchronous).
    info = await asyncio.to_thread(_tailscale_whois, ip)
    if not info:
        return {"ok": False, "reason": "not-a-tailnet-peer", "peer": peer}
    allowed = [str(s).lower() for s in allowed_logins]
    if str(info.get("loginName") or "").lower() not in allowed:
        return {
            "ok": False, "reason": "login-not-allowed",
            "peer": {**peer, **info},
        }
    return {"ok": True, "reason": "allowed", "peer": {**peer, **info}}


__all__ = [
    "extract_token", "token_matches", "normalize_peer_ip", "is_localhost",
    "authorize_peer",
]
