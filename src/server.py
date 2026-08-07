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
    config_path, effective_port, load_config, logs_dir, projects_root, save_config,
)
from logging_util import log_error  # noqa: E402
from paths import case_fold, is_path_under_roots, package_root, public_dir  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from ws import Outbox, accept_websocket  # noqa: E402

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")

# Paths served with immutable long cache (vendor assets never change content).
_VENDOR_PREFIXES = ("/vendor/",)


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Server:
    def __init__(self) -> None:
        self.config = load_config()
        self.sessions = SessionManager(self.config, lambda: save_config(self.config))
        self.pub = public_dir()
        self.pkg = package_root()
        self._gzip_cache: dict[str, bytes] = {}  # static path -> compressed body
        self._gzip_meta: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, size)
        self._ws_clients: dict[str, list] = {}  # session id -> ws objects

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
                enabled[k] = v
        gate = "none"
        if self.config.get("authToken"):
            gate = "token"
        elif self.config.get("allowedLogins"):
            gate = "tailscale"
        return {
            "roots": self.config.get("roots", []),
            "projectsRoot": projects_root,
            "tools": enabled,
            "configPath": config_path,
            "bindHost": self.config.get("bindHost", "0.0.0.0"),
            "port": effective_port(self.config.get("port")),
            "gate": gate,
        }

    def _claude_history_mtime(self, cwd: str) -> float:
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
            roots = []
            if os.name == "nt":
                import string

                for letter in string.ascii_uppercase:
                    drive = f"{letter}:\\"
                    if os.path.exists(drive):
                        roots.append({"name": f"{letter}:", "path": drive})
            else:
                roots.append({"name": "/", "path": "/"})
            home = os.path.expanduser("~")
            if home:
                roots.append({"name": f"Home ({os.path.basename(home)})", "path": home})
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
        return {
            "name": name, "cwd": cwd, "tool": tool,
            "args": str(body.get("args") or ""),
            "autostart": bool(body.get("autostart")),
        }

    # --- HTTP request dispatch --------------------------------------------------
    async def _handle_request(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        ws_owned = False
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("latin-1").strip().split(" ")
            if len(parts) < 2:
                raise HttpError(400, "Bad request line")
            method, target = parts[0], parts[1]
            headers: dict[str, str] = {}
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

            auth = await self._authorize(reader, headers, target)
            if not auth["ok"]:
                if target.startswith("/api/"):
                    await self._send_json(writer, 403, {"error": "forbidden", "reason": auth["reason"]}, headers)
                else:
                    await self._send_html(writer, 403, self._denied_page(auth))
                return

            await self._route(method, target, headers, reader, writer)
        except HttpError as err:
            await self._send_json(writer, err.status, {"error": err.message}, headers)
        except (ConnectionError, OSError):
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
            try:
                entries = self._list_dir_entries(query.get("path", [""])[0])
                return await self._send_json(writer, 200, entries, headers)
            except OSError as err:
                raise HttpError(400, str(err))

        if path == "/api/config/roots" and method == "PUT":
            body = await self._read_json(reader, headers)
            roots = body.get("roots") if isinstance(body.get("roots"), list) else []
            self.config["roots"] = [os.path.abspath(str(r)) for r in roots if str(r)]
            save_config(self.config)
            return await self._send_json(writer, 200, {"roots": self.config["roots"]}, headers)

        if path == "/api/sessions" and method == "GET":
            return await self._send_json(writer, 200, self.sessions.list(), headers)

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
            return await self._send_json(writer, 200, {"ok": ok}, headers)

        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m and method == "DELETE":
            ok = await self.sessions.remove(m.group(1))
            return await self._send_json(writer, 200, {"ok": ok}, headers)

        m = re.match(r"^/api/sessions/([^/]+)/input$", path)
        if m and method == "POST":
            body = await self._read_json(reader, headers)
            ok = self.sessions.write(m.group(1), body.get("bytes") or "")
            return await self._send_json(writer, 200, {"ok": ok}, headers)

        # --- static assets -----------------------------------------------------
        if method in ("GET", "HEAD"):
            await self._serve_static(writer, method, path, headers)
            return

        raise HttpError(405, "Method not allowed")

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
            return self._gzip_cache[cache_key], "gzip"
        compressed = _gzip.compress(body, compresslevel=6)
        if len(compressed) >= len(body):
            return body, None  # 压缩无收益（如已压缩的 WOFF2 字体）
        if cache_key is not None:
            self._gzip_cache[cache_key] = compressed
        return compressed, "gzip"

    async def _serve_static(self, writer: asyncio.StreamWriter, method: str, path: str,
                            headers: dict[str, str]) -> None:
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
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                st = os.fstat(f.fileno())
                body = f.read()
        except OSError:
            raise HttpError(404, "Not found")
        frozen = (st.st_mtime_ns, st.st_size)
        # 文件更新（如部署时替换 public/）后使压缩缓存失效，避免 gzip 与
        # 明文客户端看到不一致的内容。
        if path in self._gzip_cache and self._gzip_meta.get(path) != frozen:
            del self._gzip_cache[path]
        body, enc = self._maybe_gzip(headers, body, cache_key=path)
        if enc is not None:
            self._gzip_meta[path] = frozen
        head = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: {cache}\r\n"
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

    async def _ws_session(self, ws, sid: str) -> None:  # type: ignore[no-untyped-def]
        session = self.sessions.get(sid)
        is_agent = session is not None and session.get("engine") == "agent"
        outbox = Outbox(ws, maxlen=1024)
        outbox.start()
        try:
            def on_output(out_sid: str, chunk: bytes) -> None:
                if out_sid == sid:
                    outbox.send(chunk, binary=True)

            def on_agent_event(ev_sid: str, item: dict) -> None:
                if ev_sid == sid:
                    outbox.send(json.dumps({"type": "agent", "item": item}), binary=False)

            def on_change(s) -> None:  # type: ignore[no-untyped-def]
                if s.get("id") == sid:
                    outbox.send(json.dumps({"type": "state", "session": s}), binary=False)

            def on_reconnected(*_args) -> None:  # type: ignore[no-untyped-def]
                outbox.send(json.dumps({"type": "reconnected"}), binary=False)

            if is_agent:
                outbox.send(json.dumps({"type": "snapshot", "transcript": self.sessions.transcript(sid)}), binary=False)
                self.sessions.on("agentEvent", on_agent_event)
            else:
                recent = self.sessions.recent_output(sid)
                if recent:
                    outbox.send(recent, binary=True)
                self.sessions.on("output", on_output)
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
                            if msg.get("type") == "user" and isinstance(msg.get("text"), str):
                                self.sessions.agent_send(sid, msg["text"])
                                continue
                            if msg.get("type") == "resize" and isinstance(msg.get("cols"), (int, float)) \
                                    and isinstance(msg.get("rows"), (int, float)):
                                self.sessions.resize(sid, int(msg["cols"]), int(msg["rows"]))
                                continue
                        except json.JSONDecodeError:
                            pass
                    if not is_agent:
                        self.sessions.write(sid, payload)
                elif opcode == 0x2:  # binary
                    if not is_agent:
                        self.sessions.write(sid, payload)
        finally:
            self.sessions.off("output", on_output)
            self.sessions.off("agentEvent", on_agent_event)
            self.sessions.off("change", on_change)
            self.sessions.off("reconnected", on_reconnected)
            outbox.stop()
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
            f"Content-Length: {len(body)}\r\n"
            "Vary: Accept-Encoding\r\n"
        )
        if enc is not None:
            head += f"Content-Encoding: {enc}\r\n"
        head += "\r\n"
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    async def _send_html(self, writer: asyncio.StreamWriter, status: int, html: str) -> None:
        body = html.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {self._status_text(status)}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    @staticmethod
    def _status_text(status: int) -> str:
        return {
            200: "OK", 201: "Created", 400: "Bad Request", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
            500: "Internal Server Error",
        }.get(status, "OK")

    @staticmethod
    def _denied_page(auth: dict) -> str:
        peer = auth.get("peer") or {}
        login = peer.get("login")
        return (
            "<!doctype html><meta charset=\"utf-8\"><title>webpty</title>"
            "<body style=\"font-family:system-ui;background:#0f0f0f;color:#ededed;padding:48px;max-width:600px;margin:0 auto\">"
            "<h2 style=\"color:#3fbf7f\">webpty — access denied</h2>"
            f"<p>Peer {peer.get('ip') or '?'} {f'({login}) ' if login else ''}is not authorized.</p>"
            f"<p style=\"color:#888;font-size:13px\">Reason: {auth.get('reason')}</p>"
            "</body>"
        )


async def _serve_client(server: Server, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    await server._handle_request(reader, writer)


async def main() -> None:
    config = load_config()
    port = effective_port(config.get("port"))
    bind_host = config.get("bindHost", "0.0.0.0")

    server = Server()
    await server.sessions.init()
    server.sessions.start_host_monitor()

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve_client(server, reader, writer)

    listener = await asyncio.start_server(on_client, bind_host, port)

    print(f"[webpty] listening on http://{bind_host}:{port}", flush=True)
    print(f"[webpty] config: {config_path}", flush=True)
    if config.get("authToken"):
        print("[webpty] token gate ON — non-localhost clients must present the auth token", flush=True)
    elif config.get("allowedLogins"):
        print(f"[webpty] Tailscale identity gate ON — allowed: {', '.join(config['allowedLogins'])}", flush=True)
    else:
        print("[webpty] WARNING: no authToken and allowedLogins is empty — anyone who can reach this port can access webpty.", flush=True)
        print("[webpty]          Set config.authToken or add your Tailscale login email(s) to enable a gate.", flush=True)

    async def _autostart() -> None:
        try:
            await server.sessions.autostart()
        except Exception as err:  # noqa: BLE001
            print(f"[webpty] autostart error: {err}", flush=True)

    asyncio.create_task(_autostart())

    try:
        await listener.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        listener.close()
        await listener.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
