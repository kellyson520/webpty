"""webpty server — HTTP API + WebSocket + static SPA, stdlib only.

A small asyncio HTTP server: serves public/ (with long-lived immutable
cache for /vendor/*), exposes the JSON API, and upgrades /ws/sessions/* to
WebSocket connections for live PTY/agent data. Auth gate applies to every
request (localhost always passes).
"""
from __future__ import annotations

import asyncio
import base64
import gzip as _gzip
import json
import math
import mimetypes
import os
import re
import sys
import time
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from auth import authorize_peer  # noqa: E402
from config import (  # noqa: E402
    config_path, data_dir, effective_port, load_config, logs_dir, projects_root,
    save_config,
)
from logging_util import log_error  # noqa: E402
from paths import case_fold, is_path_under_roots, package_root, public_dir  # noqa: E402

# Module-level default resolved from env at import; Server may override via
# the data_dir constructor argument (main passes the same value).
_DATA_DIR = data_dir
from session_manager import SessionManager  # noqa: E402
from ws import Outbox, accept_websocket  # noqa: E402

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")

# Paths served with immutable long cache (vendor assets never change content).
_VENDOR_PREFIXES = ("/vendor/",)
_LOCK_FD = None  # single-instance flock fd (held in main)
MAX_WS_CONNECTIONS = 128  # audit K1: concurrent WS cap

_CLAUDE_MTIME_TTL = 30.0  # claude history mtime cache TTL (seconds)

# Tools whose native config files webpty can read & edit.
_AGENT_CONFIG_TOOLS = frozenset(
    ("codex", "reasonix", "claude", "opencode", "aider",
     "gemini", "copilot", "cursor-agent", "agy"))


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def parse_multipart(body: bytes, boundary: str) -> tuple[str | None, bytes, str]:
    """Parse a single-file multipart body (fields `file` + optional `mode`).

    Returns (filename, file_bytes, mode). filename is None / file_bytes empty
    when the file field is absent; mode falls back to "merge" when missing or
    not one of merge|replace|dry-run. Only the single trailing CRLF that
    separates the part from the boundary is stripped, so binary payloads that
    themselves end in \r or \n survive byte-for-byte.
    """
    delim = b"--" + boundary.encode()
    filename = None
    file_bytes = b""
    mode = "merge"
    # 索引化扫描:用 find 逐段定位分隔符并切片,避免 body.split(delim)
    # 对整段 body 的全量拷贝(大文件上传时 ~3x 内存峰值)。语义与
    # split 逐段处理一致:段 = delim 之后到下一个 delim 之前。
    pos = 0
    while True:
        start = body.find(delim, pos)
        if start == -1:
            break
        seg_start = start + len(delim)
        end = body.find(delim, seg_start)
        if end == -1:
            seg = body[seg_start:]
            pos = len(body)
        else:
            seg = body[seg_start:end]
            pos = end
        if b"\r\n\r\n" not in seg:
            continue
        head, _, content = seg.partition(b"\r\n\r\n")
        head_str = head.decode("utf-8", "replace")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if 'name="mode"' in head_str:
            mode = content.decode("utf-8", "replace").strip() or "merge"
        if 'name="file"' in head_str:
            for line in head_str.split("\r\n"):
                if line.lower().startswith("content-disposition:"):
                    for bit in line.split(";"):
                        bit = bit.strip()
                        if bit.startswith("filename="):
                            filename = bit[len("filename="):].strip('"')
            file_bytes = content
    if mode not in ("merge", "replace", "dry-run"):
        mode = "merge"
    return filename, file_bytes, mode


class Server:
    def __init__(self, db=None, notifier=None, cost=None, migrator=None,
                 data_dir: str | None = None, config: dict | None = None) -> None:
        # Audit H1: accept the caller's config object instead of loading a
        # second copy — two dicts made PUT /api/cost/budget and
        # PUT /api/config/* write stale session lists over the live ones
        # (restart lost sessions / budget reverted).
        self.config = config if config is not None else load_config()
        self.data_dir = data_dir if data_dir is not None else _DATA_DIR
        self.sessions = SessionManager(self.config, lambda: save_config(self.config))
        self.pub = public_dir()
        self.pkg = package_root()
        self._gzip_cache: dict[str, bytes] = {}  # static path -> compressed body
        self._gzip_cache_bytes = 0
        self._gzip_meta: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, size)
        self._asset_hash_cache: dict[str, str] = {}  # /app.js -> sha256[:16]
        self._asset_hash_meta: dict[str, tuple[int, int]] = {}
        self._ws_count = 0
        # claude history mtime cache: key = abs project path, value =
        # (mtime, cached_at). 30s TTL avoids a full scandir per /api/projects.
        self._claude_mtime_cache: dict[str, tuple[float, float]] = {}
        self.db = db
        self.notifier = notifier
        self.cost = cost
        self.migrator = migrator

    # --- helpers ------------------------------------------------------------
    def _effective_roots(self) -> list[str]:
        return list(self.config.get("roots", [])) + list(self.config.get("extraFolders", []))

    def _client_ip(self, reader: asyncio.StreamReader) -> str:
        try:
            return reader._transport.get_extra_info("peername")[0]  # type: ignore[attr-defined]
        except Exception as err:  # noqa: BLE001
            log_error("server", err)
            return ""

    async def _authorize(self, reader: asyncio.StreamReader,
                         headers: dict[str, str], url: str) -> dict:
        ip = self._client_ip(reader)
        return await authorize_peer(
            ip, headers, url,
            self.config.get("allowedLogins", []),
            self.config.get("authToken", ""),
        )

    # --- route handlers ------------------------------------------------------
    def _api_config(self) -> dict:
        enabled = {}
        for k, v in self.config.get("tools", {}).items():
            if v and isinstance(v, dict):
                enabled[k] = self._mask_api_key(v)
        gate = "none"
        if self.config.get("authToken"):
            gate = "token"
        elif self.config.get("allowedLogins"):
            gate = "tailscale"
        providers = {}
        for name, p in (self.config.get("providers") or {}).items():
            if isinstance(p, dict):
                providers[name] = self._mask_api_key(dict(p))
        return {
            "roots": self.config.get("roots", []),
            "projectsRoot": projects_root,
            "tools": enabled,
            "providers": providers,
            "configPath": config_path,
            "bindHost": self.config.get("bindHost", "0.0.0.0"),
            "port": effective_port(self.config.get("port")),
            "gate": gate,
        }

    @staticmethod
    def _mask_api_key(entry: dict) -> dict:
        """Never send plaintext apiKey to the browser (Issue 2.2). Mask to a
        'configured' marker; the agent edit form treats a masked value as
        'keep existing' and an empty string as 'clear'."""
        if isinstance(entry, dict) and entry.get("apiKey"):
            key = str(entry["apiKey"])
            entry["apiKey"] = ("****" + key[-4:]) if len(key) > 4 else "****"
        return entry

    def _claude_history_mtime(self, cwd: str) -> float:
        # 30s TTL cache: /api/projects calls this per project; the underlying
        # scandir of ~/.claude/projects/<proj> is expensive with many sessions.
        cached = self._claude_mtime_cache.get(cwd)
        if cached is not None:
            mtime, at = cached
            if time.time() - at < _CLAUDE_MTIME_TTL:
                return mtime
        proj = os.path.abspath(cwd).replace(":", "-").replace("\\", "-").replace("/", "-").replace("_", "-")
        d = os.path.join(os.path.expanduser("~"), ".claude", "projects", proj)
        mtime = 0.0
        try:
            for f in os.listdir(d):
                if not f.endswith(".jsonl"):
                    continue
                try:
                    mtime = max(mtime, os.path.getmtime(os.path.join(d, f)))
                except OSError:
                    pass
        except OSError:
            pass
        self._claude_mtime_cache[cwd] = (mtime, time.time())
        return mtime

    def _list_projects(self) -> list[dict]:
        seen = set()
        out = []

        def push(full: str) -> None:
            key = case_fold(os.path.abspath(full))
            if key in seen:
                return
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                return
            seen.add(key)
            out.append({
                "name": os.path.basename(full) or full,
                "path": os.path.abspath(full),
                "mtime": mtime * 1000,
                "claudeMtime": self._claude_history_mtime(full),
            })

        try:
            for entry in os.scandir(projects_root):
                if entry.is_dir():
                    push(entry.path)
        except OSError:
            pass
        for p in self.config.get("extraFolders", []):
            push(p)
        out.sort(key=lambda x: x["name"].lower())
        return out

    def _list_dir_entries(self, raw_path: str) -> list[dict]:
        if not raw_path:
            # Root view = the registered roots + extra folders only. Showing
            # the whole filesystem here invites navigation outside the roots,
            # which the fs/list guard then rejects with 403 ("无法列出").
            roots = []
            for r in (self.config.get("roots") or []):
                if r:
                    roots.append({"name": os.path.basename(r.rstrip("/\\")) or r,
                                  "path": r})
            for f in (self.config.get("extraFolders") or []):
                if f and f not in roots:
                    roots.append({"name": os.path.basename(f.rstrip("/\\")) or f,
                                  "path": f})
            return roots
        resolved = os.path.abspath(raw_path)
        entries = []
        for e in os.scandir(resolved):
            if e.is_dir():
                entries.append({"name": e.name, "path": e.path})
        entries.sort(key=lambda x: x["name"].lower())
        return entries

    def _validate_session_input(self, body: dict) -> dict:
        raw_cwd = str(body.get("cwd") or "")
        if not raw_cwd:
            raise HttpError(400, "cwd required")
        cwd = os.path.abspath(raw_cwd)
        tool = str(body.get("tool") or "")
        if not self.config.get("tools", {}).get(tool):
            raise HttpError(400, "Unknown tool")
        if not is_path_under_roots(cwd, self._effective_roots()):
            raise HttpError(400, "Path is outside registered roots")
        name = str(body.get("name") or os.path.basename(cwd)).strip() or os.path.basename(cwd)
        perm = body.get("permissionMode")
        if perm is not None:
            perm = str(perm)
            if perm not in ("bypassPermissions", "acceptEdits", "plan",
                            "default", "dontAsk", "fullAuto", "noAuto"):
                raise HttpError(400, f"Unknown permissionMode: {perm}")
        return {
            "name": name, "cwd": cwd, "tool": tool,
            "args": str(body.get("args") or ""),
            "autostart": bool(body.get("autostart")),
            "permissionMode": perm,
        }

    # --- HTTP request dispatch --------------------------------------------------
    async def _handle_request(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        ws_owned = False
        headers: dict[str, str] = {}
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("latin-1").strip().split(" ")
            if len(parts) < 2:
                raise HttpError(400, "Bad request line")
            method, target = parts[0], parts[1]
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()

            # WebSocket upgrade — ownership transfers to _ws_session; the
            # writer must NOT be closed by this handler's finally block.
            if headers.get("upgrade", "").lower() == "websocket":
                ws_owned = True
                await self._handle_ws_upgrade(reader, writer, target, headers)
                return

            # Static assets (HTML/JS/CSS/fonts) are always public: the
            # front-end must load to render the token-unlock screen. The gate
            # protects /api data and /ws sessions only. A bare HTML request
            # must never be answered with a 403 page before the JS can run.
            if target.startswith("/api/"):
                auth = await self._authorize(reader, headers, target)
                if not auth["ok"]:
                    await self._send_json(writer, 403,
                                          {"error": "forbidden", "reason": auth["reason"]},
                                          headers)
                    return
            # everything else (static SPA assets) → route directly

            await self._route(method, target, headers, reader, writer)
        except HttpError as err:
            await self._send_json(writer, err.status, {"error": err.message}, headers)
        except (ConnectionError, OSError):
            pass
        except Exception as err:  # noqa: BLE001 — last-resort: client gets a
            # JSON error instead of a dropped connection.
            log_error("http", err)
            try:
                await self._send_json(writer, 500, {"error": "internal server error"}, headers)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if not ws_owned:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _route(self, method: str, target: str, headers: dict[str, str],
                     reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = urllib.parse.urlparse(target).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)

        # --- API routes ------------------------------------------------------
        if path == "/api/config" and method == "GET":
            return await self._send_json(writer, 200, self._api_config(), headers)

        if path == "/api/health" and method == "GET":
            # Audit M2/M7: liveness/readiness probe for systemd/monitoring —
            # /api/config stays 200 even when the pty-host is down, which
            # made outages invisible. DB failure returns 503 so status-code
            # monitors see it.
            db_ok = False
            if self.db is not None:
                try:
                    await self.db.query_one("SELECT 1 AS x")
                    db_ok = True
                except Exception:  # noqa: BLE001
                    db_ok = False
            return await self._send_json(
                writer, 503 if not db_ok else 200, {
                    "ok": db_ok,
                    "db": db_ok,
                    "host_ready": self.sessions.host_ready,
                    "ts": time.time(),
                }, headers)

        if path == "/api/projects" and method == "GET":
            return await self._send_json(writer, 200, self._list_projects(), headers)

        if path == "/api/projects" and method == "POST":
            body = await self._read_json(reader, headers)
            raw_path = str(body.get("path") or "").strip()
            if not raw_path:
                raise HttpError(400, "path required")
            p = os.path.abspath(raw_path)
            if not os.path.isdir(p):
                raise HttpError(400, "Path does not exist" if not os.path.exists(p) else "Not a directory")
            if not isinstance(self.config.get("extraFolders"), list):
                self.config["extraFolders"] = []
            exists = any(case_fold(os.path.abspath(x)) == case_fold(p)
                         for x in self.config["extraFolders"])
            is_auto = case_fold(os.path.dirname(p)) == case_fold(os.path.abspath(projects_root))
            if not exists and not is_auto:
                self.config["extraFolders"].append(p)
                save_config(self.config)
                self._claude_mtime_cache.clear()
            return await self._send_json(writer, 200, self._list_projects(), headers)

        if path == "/api/projects/create" and method == "POST":
            body = await self._read_json(reader, headers)
            raw = str(body.get("name") or "").strip()
            target_raw = str(body.get("path") or "").strip()
            if not raw and not target_raw:
                raise HttpError(400, "name required")
            target = os.path.abspath(target_raw) if target_raw else os.path.join(os.path.abspath(projects_root), raw)
            if not is_path_under_roots(target, self._effective_roots()):
                raise HttpError(400, "Path is outside registered roots")
            if os.path.exists(target):
                raise HttpError(400, "Already exists")
            try:
                os.makedirs(target, exist_ok=True)
                if body.get("gitInit") is True:
                    import subprocess

                    try:
                        subprocess.run(["git", "init", "-b", "main"], cwd=target,
                                       capture_output=True, check=True)
                    except Exception as err:  # noqa: BLE001
                        import shutil

                        shutil.rmtree(target, ignore_errors=True)
                        raise HttpError(500, f"git init failed: {err}")
            except HttpError:
                raise
            except OSError as err:
                raise HttpError(500, f"Create failed: {err}")
            mtime = os.path.getmtime(target)
            return await self._send_json(writer, 201, {
                "name": os.path.basename(target) or target,
                "path": target,
                "mtime": mtime * 1000,
                "claudeMtime": 0,
            }, headers)

        if path == "/api/fs/list" and method == "GET":
            raw = query.get("path", [""])[0]
            if raw:
                # Directory enumeration is restricted to registered roots
                # (and their subdirs). Deny arbitrary paths like /etc.
                req_path = os.path.abspath(raw)
                allowed = [os.path.abspath(r)
                           for r in (self.config.get("roots") or [])] + \
                          [os.path.abspath(f)
                           for f in (self.config.get("extraFolders") or [])]
                if not any(req_path == a or req_path.startswith(a + os.sep)
                           for a in allowed):
                    raise HttpError(403, "path outside registered roots")
            try:
                entries = self._list_dir_entries(raw)
                return await self._send_json(writer, 200, entries, headers)
            except OSError as err:
                raise HttpError(400, str(err))

        if path == "/api/config/roots" and method == "PUT":
            body = await self._read_json(reader, headers)
            roots = body.get("roots") if isinstance(body.get("roots"), list) else []
            # If the client did NOT explicitly pass an empty list (= deny all),
            # always keep projects_root inside the roots: the new-session page
            # defaults to it and a roots set that excludes it would make every
            # default session fail with "Path is outside registered roots".
            if roots:
                normalized = [os.path.abspath(str(r)) for r in roots if str(r)]
                pr = os.path.abspath(projects_root)
                if pr not in normalized:
                    normalized.append(pr)
                self.config["roots"] = normalized
            else:
                self.config["roots"] = []
            save_config(self.config)
            self._claude_mtime_cache.clear()  # roots changed → rescan
            return await self._send_json(writer, 200, {"roots": self.config["roots"]}, headers)

        if path == "/api/config/tools" and method == "PUT":
            body = await self._read_json(reader, headers)
            incoming = body.get("tools") if isinstance(body.get("tools"), dict) else {}
            from config import DEFAULT_TOOLS
            merged = dict(self.config.get("tools", {}))
            for key, val in incoming.items():
                key = str(key)
                if val is None or val is False:
                    # Tool-level null/false = disable the whole tool.
                    merged[key] = None
                    continue
                if not isinstance(val, dict):
                    continue
                # Security: `command` determines what gets exec'd. Only allow
                # a tool's command to be one of the built-in defaults — never
                # an arbitrary path/string (that would be a remote RCE via
                # PUT /api/config/tools). New tools must use a known command.
                if "command" in val and val["command"] is not None:
                    new_cmd = str(val["command"]).strip()
                    allowed_cmds = {str(t.get("command"))
                                    for t in DEFAULT_TOOLS.values() if t}
                    if new_cmd not in allowed_cmds:
                        raise HttpError(
                            400, f"command must be one of the built-in agent "
                                 f"commands ({', '.join(sorted(allowed_cmds))})")
                base = dict(merged.get(key) or {})
                for field in ("command", "defaultArgs", "engine", "nameFlag",
                              "permissionMode", "label", "provider",
                              "apiBaseUrl", "apiKey"):
                    if field not in val:
                        continue
                    if val[field] is None:
                        # Field-level null = clear the field (back to default).
                        base.pop(field, None)
                    else:
                        base[field] = val[field]
                merged[key] = base
            self.config["tools"] = merged
            save_config(self.config)
            enabled = {k: v for k, v in merged.items()
                       if v and isinstance(v, dict)}
            return await self._send_json(writer, 200, {"tools": enabled}, headers)

        if path == "/api/config/providers" and method == "PUT":
            body = await self._read_json(reader, headers)
            incoming = body.get("providers") if isinstance(body.get("providers"), dict) else {}
            merged_providers = dict(self.config.get("providers", {}))
            for name, p in incoming.items():
                if p is None or not isinstance(p, dict):
                    # null → delete the preset
                    merged_providers.pop(name, None)
                    continue
                base = dict(merged_providers.get(name, {}))
                for field in ("baseUrl", "apiKey", "models"):
                    if field in p:
                        if p[field] is None:
                            base.pop(field, None)
                        else:
                            base[field] = p[field]
                merged_providers[name] = base
            self.config["providers"] = merged_providers
            save_config(self.config)
            return await self._send_json(writer, 200,
                                         {"providers": merged_providers}, headers)

        # --- agent native config files --------------------------------------
        if path == "/api/agent-config/list" and method == "GET":
            from agent_config import list_configs
            return await self._send_json(writer, 200, {"tools": list_configs()},
                                         headers)

        if path == "/api/agent-config/read" and method == "GET":
            from agent_config import read_config
            tool = query.get("tool", [""])[0]
            if tool not in _AGENT_CONFIG_TOOLS:
                return await self._send_json(writer, 400,
                                             {"error": "unknown tool"}, headers)
            return await self._send_json(writer, 200, read_config(tool), headers)

        if path == "/api/agent-config/update" and method == "PUT":
            from agent_config import update_config
            body = await self._read_json(reader, headers)
            tool = str(body.get("tool") or "")
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            if tool not in _AGENT_CONFIG_TOOLS:
                return await self._send_json(writer, 400,
                                             {"error": "unknown tool"}, headers)
            res = update_config(tool, values)
            status = 200 if res.get("ok") else 400
            return await self._send_json(writer, status, res, headers)

        if path == "/api/sessions" and method == "GET":
            limit_q = (query.get("limit") or [""])[0]
            limit = int(limit_q) if limit_q.isdigit() else None
            return await self._send_json(
                writer, 200, self.sessions.list(limit), headers)

        if path == "/api/sessions/order" and method == "PUT":
            body = await self._read_json(reader, headers)
            self.sessions.reorder(body.get("ids") if isinstance(body.get("ids"), list) else [])
            return await self._send_json(writer, 200, {"ok": True}, headers)

        if path == "/api/sessions" and method == "POST":
            body = await self._read_json(reader, headers)
            session = self.sessions.create(**self._validate_session_input(body))
            if body.get("start"):
                await self.sessions.start(session["id"])
            return await self._send_json(writer, 201, self.sessions.public(session["id"]), headers)

        m = re.match(r"^/api/sessions/([^/]+)/start$", path)
        if m and method == "POST":
            await self.sessions.start(m.group(1))
            return await self._send_json(writer, 200, self.sessions.public(m.group(1)), headers)

        m = re.match(r"^/api/sessions/([^/]+)/stop$", path)
        if m and method == "POST":
            ok = await self.sessions.stop(m.group(1))
            if not ok:
                raise HttpError(404, "session not found or already stopped")
            return await self._send_json(writer, 200, {"ok": True}, headers)

        m = re.match(r"^/api/sessions/([^/]+)/interrupt$", path)
        if m and method == "POST":
            # Audit C1: interrupt the current turn (SIGINT for agents,
            # Ctrl+C for pty) — graceful vs stop()'s kill.
            ok = await self.sessions.interrupt(m.group(1))
            if not ok:
                raise HttpError(409, "nothing to interrupt")
            return await self._send_json(writer, 200, {"ok": True}, headers)

        m = re.match(r"^/api/sessions/([^/]+)/reset$", path)
        if m and method == "POST":
            # Audit A1: start a brand-new agent conversation (drop --resume).
            ok = await self.sessions.reset(m.group(1))
            if not ok:
                raise HttpError(409, "only agent sessions can be reset")
            return await self._send_json(writer, 200, {"ok": True}, headers)

        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m and method == "DELETE":
            ok = await self.sessions.remove(m.group(1))
            if not ok:
                raise HttpError(404, "session not found")
            return await self._send_json(writer, 200, {"ok": True}, headers)

        m = re.match(r"^/api/sessions/([^/]+)/input$", path)
        if m and method == "POST":
            body = await self._read_json(reader, headers)
            ok = self.sessions.write(m.group(1), body.get("bytes") or "")
            if not ok:
                raise HttpError(409, "session not running")
            return await self._send_json(writer, 200, {"ok": True}, headers)

        # --- notifications -----------------------------------------------------
        if path == "/api/notify/rules" and method == "GET":
            return await self._send_json(
                writer, 200, {"rules": await self.db.list_rules()}, headers)
        if path == "/api/notify/rules" and method == "POST":
            body = await self._read_json(reader, headers)
            if not body.get("name") or not body.get("event_type"):
                raise HttpError(400, "name and event_type required")
            if not isinstance(body.get("matcher_json", "{}"), str):
                raise HttpError(400, "matcher_json must be a string")
            rid = await self.db.upsert_rule(body)
            return await self._send_json(writer, 201, {"id": rid}, headers)
        m = re.match(r"^/api/notify/rules/(\d+)$", path)
        if m and method == "PUT":
            body = await self._read_json(reader, headers)
            if not body.get("name") or not body.get("event_type"):
                raise HttpError(400, "name and event_type required")
            if not isinstance(body.get("matcher_json", "{}"), str):
                raise HttpError(400, "matcher_json must be a string")
            body["id"] = int(m.group(1))
            await self.db.upsert_rule(body)
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if m and method == "DELETE":
            await self.db.delete_rule(int(m.group(1)))
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if path == "/api/notify/messages" and method == "GET":
            raw = (query.get("page") or ["1"])[0]
            try:
                page = int(raw)
            except ValueError:
                raise HttpError(400, "Invalid page")
            return await self._send_json(
                writer, 200, await self.db.list_notifications(page), headers)
        if path == "/api/notify/test" and method == "POST":
            ok = await self.notifier.test_message()
            return await self._send_json(writer, 200, {"ok": ok}, headers)

        # --- diagnostics ----------------------------------------------------
        if path == "/api/errors" and method == "GET":
            # Audit S2: backend errors (backup/notifier/reconcile/...) were
            # stdout-only; surface the ring buffer so the UI can show them.
            from logging_util import recent_errors
            return await self._send_json(
                writer, 200, {"errors": recent_errors()}, headers)

        # --- cost -----------------------------------------------------------
        if path == "/api/cost/summary" and method == "GET":
            period = (query.get("period") or ["day"])[0]
            return await self._send_json(
                writer, 200, await self.cost.summary(period), headers)
        if path == "/api/cost/export" and method == "GET":
            # Audit 4.1: CSV export with optional absolute date range
            # (from/to as YYYY-MM-DD), else the period relative window.
            period = (query.get("period") or ["day"])[0]
            frm = (query.get("from") or [""])[0]
            to = (query.get("to") or [""])[0]
            rows = await self.cost.export_rows(period, frm or None, to or None)
            import io as _io, csv as _csv
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["ts", "session_id", "tool", "model",
                        "tokens_in", "tokens_out", "cost_usd", "source"])
            for r in rows:
                w.writerow([r.get("ts"), r.get("session_id"), r.get("tool"),
                            r.get("model"), r.get("tokens_in"), r.get("tokens_out"),
                            r.get("cost"), r.get("source")])
            data = buf.getvalue().encode("utf-8")
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/csv; charset=utf-8\r\n"
                "Content-Disposition: attachment; filename=\"webpty-cost.csv\"\r\n"
                f"Content-Length: {len(data)}\r\n"
                "\r\n"
            )
            writer.write(head.encode("latin-1"))
            writer.write(data)
            await writer.drain()
            return
        m = re.match(r"^/api/cost/by-(project|tool|model|session)$", path)
        if m and method == "GET":
            period = (query.get("period") or ["day"])[0]
            rows = await self.cost.grouped(m.group(1), period)
            return await self._send_json(writer, 200, rows, headers)
        if path == "/api/cost/alerts" and method == "GET":
            return await self._send_json(
                writer, 200, await self.cost.alerts(), headers)
        if path == "/api/cost/budget" and method == "PUT":
            body = await self._read_json(reader, headers)
            if "limit" not in body:
                raise HttpError(400, "limit required")
            try:
                limit = float(body["limit"])
            except (TypeError, ValueError):
                raise HttpError(400, "Invalid limit")
            # Audit L3: Infinity/NaN would silently disable the alert.
            if not math.isfinite(limit):
                raise HttpError(400, "Invalid limit")
            await self.cost.set_budget(limit)
            save_config(self.config)  # persist the budget across restarts
            return await self._send_json(writer, 200, {"ok": True}, headers)
        if path == "/api/cost/reconcile" and method == "POST":
            from reconciler import Reconciler
            claude_dir = os.path.expanduser("~/.claude/projects")
            rec = Reconciler(self.db, self.config)
            # File scan is blocking I/O — run it in a thread so the event
            # loop (and every other session) stays responsive.
            from reconciler import scan_claude_logs
            items = await asyncio.get_event_loop().run_in_executor(
                None, scan_claude_logs, claude_dir)
            added = 0
            for u in items:
                try:
                    added += await rec._add_one(u, "claude")
                except Exception:  # noqa: BLE001
                    continue
            return await self._send_json(writer, 200, {"added": added}, headers)

        # --- backups -----------------------------------------------------------
        if path == "/api/backup/create" and method == "POST":
            from backup import create_backup_async
            b = await create_backup_async(self.data_dir, self.config, self.db)
            return await self._send_json(writer, 201, {"backup": b}, headers)
        if path == "/api/backup/list" and method == "GET":
            from backup import list_backups
            rows = await list_backups(self.db)
            # Annotate each row with its kind (real backup vs migrate-export
            # package) so the UI can hide restore for migrate packages.
            import json as _json
            for r in rows:
                try:
                    man = _json.loads(r.get("manifest_json") or "{}")
                    r["kind"] = "migrate-export" if isinstance(man, dict) \
                        and man.get("kind") == "migrate-export" else "backup"
                except Exception:  # noqa: BLE001
                    r["kind"] = "backup"
            return await self._send_json(
                writer, 200, {"backups": rows}, headers)
        m = re.match(r"^/api/backup/restore/(\d+)$", path)
        if m and method == "POST":
            from backup import restore_backup
            # F3: sha256 + gunzip run in a thread inside restore_backup.
            res = await restore_backup(int(m.group(1)), self.data_dir, self.db,
                                       self.config)
            if res.get("ok") and isinstance(res.get("config"), dict):
                # 磁盘已写回,同步内存 config,避免运行态与磁盘分裂
                self.config.clear()
                self.config.update(res["config"])
            return await self._send_json(writer, 200, res, headers)
        m = re.match(r"^/api/backup/diff/(\d+)/(\d+)$", path)
        if m and method == "GET":
            from backup import diff_backups
            # F3: the tar gunzip/parse runs in a thread inside diff_backups.
            diff = await diff_backups(int(m.group(1)), int(m.group(2)), self.db)
            return await self._send_json(writer, 200, diff, headers)

        # --- migrate --------------------------------------------------------
        if path == "/api/migrate/export" and method == "POST":
            p = await self.migrator.export()
            return await self._send_json(writer, 201, {
                "path": p, "filename": os.path.basename(p)}, headers)
        if path == "/api/migrate/list" and method == "GET":
            return await self._send_json(
                writer, 200, {"migrations": await self.db.list_migrations()},
                headers)
        if path == "/api/migrate/clone" and method == "POST":
            body = await self._read_json(reader, headers)
            res = await self.migrator.clone(body.get("template", ""))
            return await self._send_json(writer, 200, res, headers)
        if path == "/api/migrate/import" and method == "POST":
            res = await self._handle_migrate_import(reader, headers)
            return await self._send_json(writer, 200, res, headers)
        m = re.match(r"^/api/migrate/download/([^/]+)$", path)
        if m and method == "GET":
            return await self._handle_migrate_download(writer, m.group(1))

        # --- static assets -----------------------------------------------------
        if method in ("GET", "HEAD"):
            await self._serve_static(writer, method, path, headers, query)
            return

        raise HttpError(405, "Method not allowed")

    async def _handle_migrate_download(self, writer, filename: str):
        backups_dir = os.path.join(self.data_dir, "backups")
        safe = os.path.basename(filename).replace('"', "")
        # Only the most recent export() may be downloaded — older exports and
        # arbitrary files under backups/ are never served (download oracle).
        if self.migrator is None \
                or safe != self.migrator.last_export_filename:
            return await self._send_json(
                writer, 404, {"error": "not found"}, {})
        path = os.path.realpath(os.path.join(backups_dir, safe))
        if not path.startswith(os.path.realpath(backups_dir) + os.sep) \
                or not os.path.isfile(path):
            return await self._send_json(
                writer, 404, {"error": "not found"}, {})
        try:
            size = os.path.getsize(path)
            f = await asyncio.get_running_loop().run_in_executor(
                None, lambda: open(path, "rb"))
        except OSError:
            return await self._send_json(
                writer, 404, {"error": "not found"}, {})
        headers = {"content-type": "application/gzip",
                   "content-disposition": f'attachment; filename="{safe}"',
                   "content-length": str(size),
                   "x-content-type-options": "nosniff"}
        writer.write(b"HTTP/1.1 200 OK\r\n" +
                     b"\r\n".join(f"{k}: {v}".encode() for k, v in headers.items()) +
                     b"\r\n\r\n")
        await writer.drain()
        # Audit M7: stream in 64KB chunks — reading a multi-hundred-MB
        # export into RAM would spike memory and stall other requests.
        loop = asyncio.get_running_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(None, f.read, 65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            await loop.run_in_executor(None, f.close)
        return True

    async def _handle_migrate_import(self, reader, headers) -> dict:
        length = 0
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 50 * 1024 * 1024:
            return {"status": "error", "message": "payload too large or empty"}
        body = await reader.readexactly(length)
        ct = headers.get("content-type", "")
        boundary = None
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
        if not boundary:
            return {"status": "error", "message": "missing boundary"}
        filename, file_bytes, mode = parse_multipart(body, boundary)
        if not filename or not file_bytes:
            return {"status": "error", "message": "file field missing"}
        uploads = os.path.join(self.data_dir, "uploads")
        os.makedirs(uploads, exist_ok=True)
        dest = os.path.join(uploads, os.path.basename(filename))
        with open(dest, "wb") as f:
            f.write(file_bytes)
        try:
            return await self.migrator.import_package(dest, mode)
        finally:
            try:
                os.remove(dest)
            except OSError as err:
                log_error("migrate-import", err)

    async def _read_json(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> dict:
        length = 0
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length > 256 * 1024:
            raise HttpError(413, "Payload too large")
        data = await reader.readexactly(length) if length else b"{}"
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            raise HttpError(400, "Invalid JSON")
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _accepts_gzip(accept_encoding: str) -> bool:
        """True if the client may receive gzip (RFC 9110 q-values, case-insensitive).

        `gzip;q=0` — or `*;q=0` with no explicit gzip — means "do not send
        gzip"; an explicit `gzip;q=0` wins over a positive `*` wildcard.
        """
        seen_gzip = False
        gzip_q = 0.0
        wildcard_q = 0.0
        for part in accept_encoding.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, params = part.partition(";")
            q = 1.0
            for p in params.split(";"):
                p = p.strip()
                if not p:
                    continue
                k, _, v = p.partition("=")
                if k.strip().lower() == "q":
                    try:
                        q = float(v.strip())
                    except ValueError:
                        q = 0.0
            name = name.strip().lower()
            if name == "gzip":
                seen_gzip = True
                gzip_q = max(gzip_q, q)
            elif name == "*":
                wildcard_q = max(wildcard_q, q)
        if seen_gzip:
            return gzip_q > 0
        return wildcard_q > 0

    def _cache_gzip(self, cache_key: str | None, value) -> None:  # type: ignore[no-untyped-def]
        """Store a gzip-cache entry with a byte budget (audit L5): over 8MB
        the whole cache is dropped — static files are few and re-compressing
        one is cheaper than unbounded memory."""
        if cache_key is None:
            return
        if value is None:
            self._gzip_cache[cache_key] = None  # negative cache
            return
        if self._gzip_cache_bytes + len(value) > 8 * 1024 * 1024:
            self._gzip_cache.clear()
            self._gzip_cache_bytes = 0
        self._gzip_cache[cache_key] = value
        self._gzip_cache_bytes += len(value)

    def _maybe_gzip(self, headers: dict[str, str], body: bytes,
                    cache_key: str | None = None) -> tuple[bytes, str | None]:
        """Compress body with gzip when it pays off; None encoding means identity.

        Only bodies larger than 1 KB are compressed, and only when the client
        accepts gzip and the compressed form is actually smaller. Static assets
        cache their compressed form in `self._gzip_cache` (keyed by request
        path), so repeat requests skip recompression. JSON responses pass
        cache_key=None and are never cached (their content changes).
        """
        if len(body) <= 1024:
            return body, None
        if not self._accepts_gzip(headers.get("accept-encoding", "")):
            return body, None
        if cache_key is not None and cache_key in self._gzip_cache:
            cached = self._gzip_cache[cache_key]
            if cached is not None:
                return cached, "gzip"
            return body, None  # negative cache: known incompressible
        # Skip compression entirely for formats that are already compressed:
        # attempting WOFF2 (1.4MB) at level 6 costs ~100-300ms of SYNCHRONOUS
        # event-loop time per request — and it never pays off, so without
        # this check every request re-compressed it (no negative cache).
        ctype = mimetypes.guess_type(cache_key or "")[0] or ""
        if ctype.split("/")[0] in ("font", "image", "audio", "video") \
                or ctype in ("application/zip", "application/gzip", "application/pdf"):
            if cache_key is not None:
                self._cache_gzip(cache_key, None)  # negative cache
            return body, None
        compressed = _gzip.compress(body, compresslevel=6)
        if len(compressed) >= len(body):
            if cache_key is not None:
                self._cache_gzip(cache_key, None)  # negative cache
            return body, None  # 压缩无收益（如已压缩的 WOFF2 字体）
        if cache_key is not None:
            self._cache_gzip(cache_key, compressed)
        return compressed, "gzip"

    async def _serve_static(self, writer: asyncio.StreamWriter, method: str, path: str,
                            headers: dict[str, str], query: dict | None = None) -> None:
        if path == "/":
            path = "/index.html"
        # Prevent path traversal.
        safe = os.path.normpath(path).lstrip("/")
        if safe.startswith("..") or ".." in safe.split("/"):
            raise HttpError(403, "Forbidden")
        full = os.path.join(self.pub, safe)
        if not os.path.isfile(full):
            raise HttpError(404, "Not found")
        cache = "no-store"
        if path.startswith(_VENDOR_PREFIXES):
            cache = "public, max-age=604800, immutable"
        elif path in ("/app.js", "/styles.css") and (query or {}).get("v"):
            # Versioned app assets (?v=<content-hash>): the URL changes when
            # the file changes, so they may be cached immutably — no-store was
            # forcing a re-download of ~400KB per page load.
            import hashlib
            try:
                with open(full, "rb") as _f:
                    h = hashlib.sha256(_f.read()).hexdigest()[:16]
                if (query.get("v") or [""])[0] == h:
                    cache = "public, max-age=604800, immutable"
            except OSError:
                pass
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                st = os.fstat(f.fileno())
                # ETag/304 (audit V6): weak etag from mtime+size costs
                # nothing and answers hard refreshes (Ctrl+F5) without
                # reading the whole 1.4MB woff2/app.js.
                etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
                inm = headers.get("if-none-match", "")
                if inm and etag in inm.split(","):
                    head = (
                        "HTTP/1.1 304 Not Modified\r\n"
                        f"ETag: {etag}\r\n"
                        f"Cache-Control: {cache}\r\n"
                        "\r\n"
                    )
                    writer.write(head.encode("latin-1"))
                    await writer.drain()
                    return
                body = f.read()
        except OSError:
            raise HttpError(404, "Not found")
        frozen = (st.st_mtime_ns, st.st_size)
        # 文件更新（如部署时替换 public/）后使压缩缓存失效，避免 gzip 与
        # 明文客户端看到不一致的内容。
        if path in self._gzip_cache and self._gzip_meta.get(path) != frozen:
            del self._gzip_cache[path]
        # Inject ?v=<content-hash> into the app asset references so browsers
        # can cache them immutably while a deploy still busts the cache
        # (hash changes with the bytes). index.html itself stays no-store.
        if path == "/index.html":
            import hashlib as _hl
            for asset in ("/app.js", "/styles.css"):
                asset_path = os.path.join(self.pub, asset.lstrip("/"))
                try:
                    # Cache the hash by (mtime, size) — recomputing sha256 of
                    # app.js on every index.html hit re-reads ~130KB.
                    asset_st = os.stat(asset_path)
                    asset_frozen = (asset_st.st_mtime_ns, asset_st.st_size)
                    h = self._asset_hash_cache.get(asset)
                    if h is None or self._asset_hash_meta.get(asset) != asset_frozen:
                        with open(asset_path, "rb") as _f:
                            h = _hl.sha256(_f.read()).hexdigest()[:16]
                        self._asset_hash_cache[asset] = h
                        self._asset_hash_meta[asset] = asset_frozen
                    body = body.replace(
                        f'href="{asset}"'.encode(),
                        f'href="{asset}?v={h}"'.encode())
                    body = body.replace(
                        f'src="{asset}"'.encode(),
                        f'src="{asset}?v={h}"'.encode())
                except OSError:
                    pass
        body, enc = self._maybe_gzip(headers, body, cache_key=path)
        if enc is not None:
            self._gzip_meta[path] = frozen
        head = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ctype}\r\n"
            # Audit L1: hardening headers on static assets too.
            "X-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: {cache}\r\n"
            f"ETag: {etag}\r\n"
            "Vary: Accept-Encoding\r\n"
        )
        if enc is not None:
            head += f"Content-Encoding: {enc}\r\n"
        head += "\r\n"
        writer.write(head.encode("latin-1"))
        if method == "GET":
            writer.write(body)
        await writer.drain()

    # --- WebSocket -------------------------------------------------------------
    async def _handle_ws_upgrade(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter, target: str,
                                 headers: dict[str, str]) -> None:
        # Origin check (Issue 3.4, defense in depth): reject cross-site
        # WebSocket connections so a malicious page can't ride the user's
        # session cookie to open a terminal. Browsers send Origin on WS;
        # missing Origin (non-browser clients) is allowed for tooling.
        origin = headers.get("origin") or headers.get("Origin")
        if origin:
            host = headers.get("host") or headers.get("Host") or ""
            try:
                from urllib.parse import urlparse as _up
                o_host = _up(origin).netloc
                if o_host and o_host != host and o_host not in ("localhost", "127.0.0.1"):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
            except ValueError:
                pass
        # Audit K1: cap concurrent WS connections (each holds recv + drain +
        # heartbeat tasks). With the token gate off and the port reachable,
        # an unbound peer could exhaust fds — refuse past MAX_WS.
        if self._ws_count >= MAX_WS_CONNECTIONS:
            writer.write(b"HTTP/1.1 429 Too Many Requests\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self._ws_count += 1
        path = urllib.parse.urlparse(target).path
        m = re.match(r"^/ws/sessions/([^/]+)$", path)
        if not m:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        raw_id = m.group(1)
        # Match JS decodeURIComponent semantics: '%' not followed by two hex
        # digits is malformed and must be rejected (Python's unquote is
        # lenient, which would let '%zz' through as a literal).
        if re.search(r"%(?![0-9a-fA-F]{2})", raw_id):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            sid = urllib.parse.unquote(raw_id)
        except (ValueError, UnicodeDecodeError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        auth = await self._authorize(reader, headers, target)
        if not auth["ok"]:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        ws = await accept_websocket(reader, writer, headers, target)
        if ws is None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        asyncio.create_task(self._ws_session(ws, sid))

    def _input_offline_hint(self, sid: str, outbox) -> None:
        """Audit T3: throttle one terminal-visible notice per 5s while the
        pty-host is unreachable (write() failing)."""
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_offline_hint_at", 0.0) < 5.0:
            return
        self._offline_hint_at = now
        hint = ("\r\n[webpty] pty-host 不可达，输入暂未送达——正在自动重连…\r\n"
                "\x1b[K").encode("utf-8")
        try:
            outbox.send(hint, binary=True)
        except Exception:  # noqa: BLE001
            pass

    async def _send_snapshot(self, outbox, sid: str) -> None:
        """Chunked transcript snapshot (audit 8.1/N1): serialize in the
        executor, send ~256KB frames; the frontend accumulates + parses
        once. Used on connect AND on resync after dropped frames."""
        transcript = self.sessions.transcript(sid)
        loop = asyncio.get_event_loop()
        encoded = await loop.run_in_executor(
            None, lambda: json.dumps(transcript))
        CHUNK = 256 * 1024
        for i in range(0, len(encoded), CHUNK):
            outbox.send(json.dumps({
                "type": "snapshot",
                "chunk": encoded[i:i + CHUNK],
                "done": i + CHUNK >= len(encoded),
            }), binary=False)

    async def _ws_session(self, ws, sid: str) -> None:  # type: ignore[no-untyped-def]
        session = self.sessions.get(sid)
        is_agent = session is not None and session.get("engine") == "agent"
        # Audit S1a: protocol version handshake — new frontends ignore
        # unknown JSON silently, so this frame is harmless to old ones.
        ws.send_text(json.dumps({"type": "proto", "v": 1}))
        # Replay window state (pty sessions): bytes of recent_output already
        # delivered. Declared here so on_resync (defined for both engines)
        # can reset them via nonlocal.
        skip = 0
        seen = 0

        def on_resync() -> None:
            # Frames were dropped (consumer too slow, e.g. backgrounded tab).
            # Send the full buffer snapshot; the client resets the terminal
            # and replays it so incremental TUI state is rebuilt instead of
            # showing a garbled, misaligned screen.
            try:
                if is_agent:
                    # Audit N1: agent sessions had NO resync path — dropped
                    # outbox frames silently lost transcript entries. Resend
                    # the full transcript as a snapshot (same path as the
                    # initial connect, chunked). Fired async: on_resync is a
                    # sync callback and _send_snapshot awaits the executor.
                    asyncio.create_task(self._send_snapshot(outbox, sid))
                    return
                recent = self.sessions.recent_output(sid)
                if recent:
                    import base64 as _b64
                    outbox.send(json.dumps({
                        "type": "resync",
                        "data": _b64.b64encode(recent).decode("ascii"),
                    }), binary=False)
                # The snapshot already contains the buffer; reset the replay
                # window so subsequent live frames are NOT skipped.
                nonlocal skip, seen
                skip = 0
                seen = 0
            except Exception:  # noqa: BLE001
                pass

        outbox = Outbox(ws, maxlen=256, on_resync=on_resync)
        outbox.start()
        try:
            def on_agent_event(ev_sid: str, item: dict) -> None:
                if ev_sid == sid:
                    outbox.send(json.dumps({"type": "agent", "item": item}), binary=False)

            def on_change(s) -> None:  # type: ignore[no-untyped-def]
                if s.get("id") == sid:
                    outbox.send(json.dumps({"type": "state", "session": s}), binary=False)

            def on_reconnected(*_args) -> None:  # type: ignore[no-untyped-def]
                outbox.send(json.dumps({"type": "reconnected"}), binary=False)

            # Heartbeat: ping every 25s; if no PONG for 60s the connection is
            # half-open (backgrounded tab, network partition) — close it so
            # the client reconnects and resyncs instead of freezing.
            async def _heartbeat() -> None:
                import time as _t
                ws._last_pong_at = _t.monotonic()
                try:
                    while True:
                        # Adaptive cadence (audit L1): an idle tab gets a ping
                        # every 60s instead of 25s — ~3500 2-byte frames/day
                        # per idle connection saved; active sessions keep the
                        # 25s cadence because their own traffic is the
                        # keepalive. Timeout scales with the interval.
                        idle = _t.monotonic() - ws._last_activity_at
                        interval = 60.0 if idle > 30 else 25.0
                        await asyncio.sleep(interval)
                        if ws._closed:
                            return
                        ws.ping()
                        timeout = 150.0 if interval == 60.0 else 60.0
                        if _t.monotonic() - ws.last_pong_at() > timeout:
                            await ws.close(1001, "heartbeat timeout")
                            return
                except (asyncio.CancelledError, ConnectionError, OSError):
                    pass

            hb_task = asyncio.get_event_loop().create_task(_heartbeat())

            if is_agent:
                # Audit 8.1: a 4000-item transcript can be MBs — one frame
                # blocked the event loop (json.dumps) and the client's parse
                # (single JSON.parse long task). Chunk to ~256KB frames and
                # serialize in the executor (shared with resync).
                await self._send_snapshot(outbox, sid)
                self.sessions.on("agentEvent", on_agent_event)
            else:
                recent = self.sessions.recent_output(sid)
                if not recent:
                    # Audit S2: after a restart the ring buffer is empty —
                    # recover the last 128KB from disk so the terminal isn't
                    # blank. pty-host's own replay may overlap; the skip
                    # window below dedupes what the host re-broadcasts.
                    recent = self.sessions.tail_log(sid)
                if recent:
                    outbox.send(recent, binary=True)
                # Skip the replay window: pty-host broadcasts the buffer
                # snapshot to every attached client on reconnect, which would
                # re-send what `recent` already covered and duplicate the
                # terminal content. Track the byte offset already delivered.
                skip = len(recent) if recent else 0
                seen = 0
                def on_output(out_sid: str, chunk: bytes) -> None:
                    nonlocal seen, skip
                    if out_sid != sid:
                        return
                    if seen < skip:
                        take = min(skip - seen, len(chunk))
                        seen += take
                        chunk = chunk[take:]
                        if not chunk:
                            return
                    outbox.send(chunk, binary=True)
                self.sessions.on("output", on_output)
                def on_resync(resync_sid: str, snapshot: bytes | None = None) -> None:
                    nonlocal skip, seen
                    if resync_sid == sid:
                        if snapshot is not None:
                            # host reattach replay: send the snapshot as a
                            # resync frame directly (frontend wipes+replays).
                            import base64 as _b64
                            outbox.send(json.dumps({
                                "type": "resync",
                                "data": _b64.b64encode(snapshot).decode("ascii"),
                            }), binary=False)
                            skip = 0
                            seen = 0
                        else:
                            # pty-host dropped our pipe: outbox.resync() makes
                            # the next drain emit the resync frame instead of
                            # a silent output gap.
                            outbox.resync()
                self.sessions.on("resync", on_resync)
            self.sessions.on("change", on_change)
            self.sessions.on("reconnected", on_reconnected)

            while True:
                frame = await ws.recv()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x1:  # text
                    text = payload.decode("utf-8", "replace")
                    if text.startswith("{"):
                        try:
                            msg = json.loads(text)
                            # Audit S1c: control messages must carry
                            # __ctl:true — otherwise a user typing valid
                            # JSON like {"type":"resize"} into bash would be
                            # hijacked as a control message and swallowed.
                            if msg.get("__ctl") is not True:
                                raise ValueError("not a control message")
                            if msg.get("type") == "user" and isinstance(msg.get("text"), str):
                                self.sessions.agent_send(sid, msg["text"])
                                continue
                            if msg.get("type") == "resize" and isinstance(msg.get("cols"), (int, float)) \
                                    and isinstance(msg.get("rows"), (int, float)):
                                self.sessions.resize(sid, int(msg["cols"]), int(msg["rows"]))
                                continue
                            # Audit L1: an unknown __ctl type must never fall
                            # through into the terminal as literal text.
                            continue
                        except (json.JSONDecodeError, ValueError):
                            pass
                    if not is_agent:
                        if not self.sessions.write(sid, payload):
                            # Audit T3: pty-host down (crash window) silently
                            # swallowed input — surface it once so the user
                            # knows typing isn't reaching the shell.
                            self._input_offline_hint(sid, outbox)
                elif opcode == 0x2:  # binary
                    if not is_agent:
                        if not self.sessions.write(sid, payload):
                            self._input_offline_hint(sid, outbox)
        finally:
            self.sessions.off("output", on_output)
            self.sessions.off("agentEvent", on_agent_event)
            self.sessions.off("change", on_change)
            self.sessions.off("reconnected", on_reconnected)
            hb_task.cancel()
            outbox.stop()
            if self._ws_count > 0:
                self._ws_count -= 1
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # --- response helpers ---------------------------------------------------------
    async def _send_json(self, writer: asyncio.StreamWriter, status: int, obj: dict,
                         headers: dict[str, str]) -> None:
        body = json.dumps(obj).encode("utf-8")
        body, enc = self._maybe_gzip(headers, body)
        head = (
            f"HTTP/1.1 {status} {self._status_text(status)}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            # Audit L1: hardening headers on all JSON responses.
            "X-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Vary: Accept-Encoding\r\n"
        )
        if enc is not None:
            head += f"Content-Encoding: {enc}\r\n"
        head += "\r\n"
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    @staticmethod
    def _status_text(status: int) -> str:
        return {
            200: "OK", 201: "Created", 400: "Bad Request", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
            500: "Internal Server Error",
        }.get(status, "OK")


async def _serve_client(server: Server, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    await server._handle_request(reader, writer)


def _backup_settings(config: dict) -> tuple[float, int]:
    """Parse backup interval_hours/retention defensively; fall back to
    defaults (24h / 7) and log on non-numeric config values or a non-dict
    `backup` section so a bad value can never kill the loop."""
    backup_cfg = config.get("backup")
    if not isinstance(backup_cfg, dict):
        backup_cfg = {}
    try:
        interval = float(backup_cfg.get("interval_hours", 24))
        retention = int(backup_cfg.get("retention", 7))
        return interval, retention
    except (TypeError, ValueError) as err:
        log_error("backup", err)
        return 24.0, 7


async def _notify_retry_loop(notifier, interval_s: float = 300.0) -> None:
    """Periodically retry undelivered SMTP notifications (delivered=0 rows
    after a transient SMTP failure). Errors are logged, never fatal —
    mirrors the _backup_loop pattern."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await notifier.send_pending()
        except Exception as err:  # noqa: BLE001
            log_error("notifier", err)


async def main() -> None:
    config = load_config()
    port = effective_port(config.get("port"))
    bind_host = config.get("bindHost", "0.0.0.0")

    # Audit G/H: fail with a clear message before starting anything if the
    # data directory isn't writable (DB, logs, backups all live there).
    for d in (data_dir, logs_dir):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".webpty-write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
        except OSError as err:
            print(f"[webpty] data dir not writable: {d}: {err}", flush=True)
            raise SystemExit(1)

    # Single-instance lock (audit V3): two servers (e.g. a manual
    # --port run beside systemd) would each spawn their own pty-host
    # fighting over the Unix socket and overwrite config.json's session
    # list. flock is advisory + atomic; released on process exit.
    import fcntl
    lock_path = os.path.join(os.path.dirname(config_path), "webpty.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[webpty] another instance is already running — exiting", flush=True)
        raise SystemExit(1)
    global _LOCK_FD
    _LOCK_FD = lock_fd

    from cost_tracker import CostTracker
    from db import Database
    from notifier import Notifier
    db = Database(os.path.join(data_dir, "webpty.db"))
    db.connect()
    notifier = Notifier(db, config)
    cost = CostTracker(db, config)
    from migrator import Migrator
    migrator = Migrator(data_dir, config, db)
    server = Server(db=db, notifier=notifier, cost=cost, migrator=migrator,
                    data_dir=data_dir, config=config)
    await server.sessions.init()
    server.sessions.start_host_monitor()
    server.sessions.start_stall_monitor()
    server.sessions.on("session_event", notifier.handle_event)
    server.sessions.on("session_event", cost.on_session_event)
    server.sessions.on("agentEvent", lambda sid, item: cost.handle_agent_event(item, sid))

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve_client(server, reader, writer)

    # Secure-by-default: refuse to listen on non-loopback addresses while the
    # gate is off. With bindHost=0.0.0.0 and no authToken/allowedLogins, any
    # LAN/internet client can rewrite tools.command and run arbitrary commands
    # (RCE). Local-only is always fine; remote access REQUIRES a gate.
    _loopbacks = ("127.0.0.1", "::1", "localhost")
    if bind_host not in _loopbacks and not config.get("authToken") \
            and not config.get("allowedLogins"):
        raise SystemExit(
            "[webpty] REFUSING to start: bindHost is not loopback but no "
            "authToken/allowedLogins is configured (remote RCE risk). "
            "Set config.authToken (or allowedLogins) or bind 127.0.0.1.")

    try:
        listener = await asyncio.start_server(on_client, bind_host, port)
    except OSError as err:
        # Audit G: EADDRINUSE / permission errors deserve a clear message.
        print(f"[webpty] cannot bind http://{bind_host}:{port}: {err}", flush=True)
        if getattr(err, "errno", None) == 98:  # EADDRINUSE
            print("[webpty]   Port already in use — another webpty instance, "
                  "or set WEBPTY_PORT to change the port.", flush=True)
        raise SystemExit(1)

    print(f"[webpty] listening on http://{bind_host}:{port}", flush=True)
    print(f"[webpty] config: {config_path}", flush=True)
    if config.get("authToken"):
        print("[webpty] token gate ON — non-localhost clients must present the auth token", flush=True)
    elif config.get("allowedLogins"):
        print(f"[webpty] Tailscale identity gate ON — allowed: {', '.join(config['allowedLogins'])}", flush=True)
    else:
        print("[webpty] WARNING: no authToken and allowedLogins is empty — anyone who can reach this port can access webpty.", flush=True)
        print("[webpty]          Set config.authToken or add your Tailscale login email(s) to enable a gate.", flush=True)

    async def _backup_loop() -> None:
        """Periodic auto-backup: first run 30s after startup, then every
        backup.interval_hours (default 24h); errors are logged, never fatal.
        """
        from backup import create_backup_async, rotate

        await asyncio.sleep(30)  # 启动后 30s 首次
        while True:
            interval, retention = _backup_settings(config)
            # Audit D1/D2: rotate runs even when create fails (stale
            # packages must still be cleaned); a failed create retries in
            # 10min instead of waiting a full interval; NaN/∞ intervals are
            # clamped so the loop can never die.
            import math as _m
            interval = max(interval if _m.isfinite(interval) else 24.0, 0.1)
            ok = True
            try:
                await create_backup_async(data_dir, config, db)
            except Exception as err:  # noqa: BLE001
                log_error("backup", err)
                ok = False
            try:
                await rotate(db, retention)
            except Exception as err:  # noqa: BLE001
                log_error("backup-rotate", err)
            await asyncio.sleep((interval if ok else 10 / 60) * 3600)

    async def _autostart() -> None:
        try:
            await server.sessions.autostart()
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] autostart error: {err}", flush=True)

    async def _prune_loop() -> None:
        """Daily retention sweep + WAL checkpoint — bounded DB growth."""
        await asyncio.sleep(90)  # after the first backup run
        while True:
            try:
                result = await db.prune_old_data()
                if result.get("deleted_notifications") or result.get("deleted_usage"):
                    print(f"[webpty] pruned old data: {result}", flush=True)
            except Exception as err:  # noqa: BLE001
                log_error("prune", err)
            await asyncio.sleep(86400)  # daily

    backup_task = asyncio.create_task(_backup_loop())
    prune_task = asyncio.create_task(_prune_loop())
    # 定时重试未送达的 SMTP 通知(默认每 5 分钟),失败只记日志不退出
    notify_task = asyncio.create_task(_notify_retry_loop(notifier))
    autostart_task = asyncio.create_task(_autostart())

    async def _budget_loop() -> None:
        """Audit C: budget flip → notification (every 5 min, cheap query)."""
        from notifier import Notifier  # noqa: F401

        async def _on_flip(over: bool) -> None:
            if over:
                notifier.handle_event({
                    "type": "budget_over", "session_id": "", "name": "webpty",
                    "tool": "cost", "project": "", "state": "over",
                    "exit_code": None, "signal": None, "ts": time.time()})
            else:
                notifier.handle_event({
                    "type": "budget_ok", "session_id": "", "name": "webpty",
                    "tool": "cost", "project": "", "state": "ok",
                    "exit_code": None, "signal": None, "ts": time.time()})

        await asyncio.sleep(30)
        while True:
            try:
                await cost.check_budget(_on_flip)
            except Exception as err:  # noqa: BLE001
                log_error("budget", err)
            await asyncio.sleep(300)

    budget_task = asyncio.create_task(_budget_loop())

    try:
        await listener.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        # Audit M1: cancel ALL background loops before tearing down the db
        # so no in-flight backup/retry/prune/budget task touches a closed
        # connection.
        for task in (backup_task, notify_task, prune_task,
                     budget_task, autostart_task):
            task.cancel()
        await asyncio.gather(backup_task, notify_task, prune_task,
                             budget_task, autostart_task,
                             return_exceptions=True)
        server.sessions.stop_host_monitor()
        server.sessions.stop_stall_monitor()
        await server.sessions.stop_host()
        listener.close()
        await listener.wait_closed()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
