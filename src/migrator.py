"""One-click config migration & environment clone (single-node; cluster
preview via WorkerInterface). Business-management layer.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
import uuid

from logging_util import log_error

# Max uncompressed size of a single member read from a migration package.
# Imported/cloned packages are untrusted input: a crafted tar can declare a
# huge member and exhaust memory when read into RAM (zip-bomb style DoS).
MAX_PACKAGE_MEMBER_SIZE = 64 * 1024 * 1024


def _is_sensitive(key: str) -> bool:
    """True for config keys carrying credentials: exact matches (authToken,
    allowedLogins) plus any key whose name contains password/key/token
    (covers notify.smtp.password, backup.encryption_key, apiKey, ...)."""
    if key in ("authToken", "allowedLogins"):
        return True
    k = key.lower()
    return "password" in k or "key" in k or "token" in k


def redact_config(obj):
    """Deep-copy `obj` replacing sensitive values with "" (the input is never
    mutated). Used when exporting state so secrets never leave the host in
    plaintext; non-sensitive keys pass through unchanged."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive(k):
                # 敏感键值一律置空(嵌套 dict 递归保留非敏感子键)
                out[k] = redact_config(v) if isinstance(v, dict) else ""
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(obj, list):
        return [redact_config(x) for x in obj]
    return obj


def sanitize_import_config(cfg: dict) -> dict:
    """Filter an imported config so it can never inject credentials or
    executable fields (Issue 1: RCE / auth takeover via migrate import).

    - credentials (authToken / allowedLogins / *key* / *password* / *token*)
      are dropped entirely (the receiver keeps its own)
    - sessions (runtime state) is never imported — prevents ghost sessions
      after migrate import / backup restore
    - tools: `command` must be a built-in default command; otherwise the tool
      entry is dropped
    - providers: apiKey is dropped
    """
    from config import DEFAULT_TOOLS
    allowed_cmds = {str(t.get("command"))
                    for t in DEFAULT_TOOLS.values() if t}
    out = {}
    for k, v in cfg.items():
        if _is_sensitive(k) or k == "sessions":
            continue  # 凭据 + 运行时会话列表永不导入
        if k == "tools" and isinstance(v, dict):
            tools = {}
            for name, tv in v.items():
                if not isinstance(tv, dict):
                    continue
                t = dict(tv)
                cmd = str(t.get("command") or "")
                if cmd and cmd not in allowed_cmds:
                    continue  # drop tools with non-builtin commands
                t.pop("apiKey", None)
                tools[name] = t
            out[k] = tools
            continue
        if k == "providers" and isinstance(v, dict):
            providers = {}
            for name, pv in v.items():
                if isinstance(pv, dict):
                    p = dict(pv)
                    p.pop("apiKey", None)
                    providers[name] = p
            out[k] = providers
            continue
        out[k] = v
    return out


class WorkerInterface:
    """Cluster reservation: a controller aggregates export_state() from each
    executor; single-node deployments implement this directly."""

    def export_state(self) -> dict:
        raise NotImplementedError

    def import_state(self, state: dict, mode: str) -> dict:
        raise NotImplementedError


class Migrator(WorkerInterface):
    def __init__(self, data_dir: str, config: dict, db) -> None:
        self.data_dir = data_dir
        self.config = config
        self.db = db
        # Basename of the most recent export() — the only file the download
        # endpoint may serve (exports never linger for arbitrary reads).
        self.last_export_filename: str | None = None

    def source_node_id(self) -> str:
        path = os.path.join(self.data_dir, "node_id")
        try:
            with open(path, encoding="utf-8") as f:
                nid = f.read().strip()
            if nid:
                return nid
        except OSError:
            pass
        nid = uuid.uuid4().hex[:16]
        with open(path, "w", encoding="utf-8") as f:
            f.write(nid)
        return nid

    async def export(self) -> str:
        from backup import collect_state
        state = await collect_state(self.data_dir, self.config, self.db)
        # Secrets (authToken, smtp passwords, encryption keys, ...) are
        # redacted before packaging — the export must never carry them in
        # plaintext. Consequence: importing a package yields empty secrets
        # that must be re-entered on the target host.
        state = redact_config(state)
        manifest = {
            "schema_version": 1,
            "created_at": time.time(),
            "source_node_id": self.source_node_id(),
            "content": ["config", "notify_rules", "sessions", "prices"],
            "secrets_redacted": True,
        }
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, obj in (("manifest.json", manifest),
                              ("state.json", state)):
                data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        backups_dir = os.path.join(self.data_dir, "backups")
        os.makedirs(backups_dir, exist_ok=True)
        path = os.path.join(
            backups_dir, f"webpty-migrate-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        self.last_export_filename = os.path.basename(path)
        # 登记 backups 供 rotate 清理(防孤儿文件);登记失败仅记日志,不阻断
        # 导出返回(否则文件已落盘却 500,反而制造孤儿)。秒级文件名可能碰撞:
        # 先删同 filename 旧行,避免悬空引用(rotate 删文件后旧行指向已删
        # 文件,restore 报 file missing)。
        try:
            await self.db.delete_backup_by_filename(os.path.basename(path))
            await self.db.add_backup({
                "filename": os.path.basename(path),
                "size_bytes": os.path.getsize(path),
                "sha256": "",
                "manifest_json": json.dumps({
                    "kind": "migrate-export",
                    "created_at": time.time()}, sort_keys=True),
                "encrypted": 0, "retained": 1})
        except Exception as err:  # noqa: BLE001
            log_error("migrator", err)
        return path

    def _read_package(self, path: str) -> dict | None:
        """Read manifest.json + state.json from a package. Path-traversal and
        decompression-bomb safe: only fixed member names are read, nothing is
        extracted to disk, and members larger than MAX_PACKAGE_MEMBER_SIZE
        are rejected before any bytes are read."""
        try:
            with tarfile.open(path, "r:gz") as tf:
                man_info = tf.getmember("manifest.json")
                st_info = tf.getmember("state.json")
                if man_info.size > MAX_PACKAGE_MEMBER_SIZE \
                        or st_info.size > MAX_PACKAGE_MEMBER_SIZE:
                    return None
                man_raw = tf.extractfile(man_info)
                st_raw = tf.extractfile(st_info)
                if man_raw is None or st_raw is None:
                    return None
                manifest = json.loads(man_raw.read())
                state = json.loads(st_raw.read())
        except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError):
            return None
        return {"manifest": manifest, "state": state}

    async def import_package(self, path: str, mode: str = "merge") -> dict:
        pkg = self._read_package(path)
        if not pkg:
            return {"status": "error", "message": "invalid package",
                    "mode": mode}
        state = pkg["state"]
        incoming = state.get("config") or {}
        if not isinstance(incoming, dict):
            return {"status": "error", "message": "invalid package",
                    "mode": mode}
        # Security: sanitize the imported config BEFORE applying it. Never
        # import credentials or executable fields from an untrusted package —
        # that would be config injection (RCE / auth takeover).
        incoming = sanitize_import_config(incoming)
        if mode == "dry-run":
            current = dict(self.config)
            changed = []
            for k, v in incoming.items():
                if current.get(k) != v:
                    if _is_sensitive(k):
                        # 只告知键会变化,绝不回显新旧 secret 值
                        changed.append({"key": k, "incoming": "redacted"})
                    else:
                        changed.append({"key": k, "incoming": v})
            return {"status": "dry-run", "mode": mode, "changes": changed}
        if mode == "replace":
            self.config.clear()
            self.config.update(incoming)
        else:  # merge (default): package values win for keys it carries,
            # existing keys absent from the package are kept (no clear)
            self.config.update(incoming)
        # prices 已随 config 合并;若包只在顶层携带 prices 则补进 config
        if "prices" not in self.config and isinstance(state.get("prices"),
                                                      dict):
            self.config["prices"] = state["prices"]
        # 导入 notify_rules:同 id 覆盖,其余新增(sessions 是运行时状态,
        # 不迁移)
        existing_ids = {r["id"] for r in await self.db.list_rules()}
        for rule in state.get("notify_rules") or []:
            if not isinstance(rule, dict) or not rule.get("name") \
                    or not rule.get("event_type"):
                continue
            r = dict(rule)
            if r.get("id") not in existing_ids:
                r.pop("id", None)
            await self.db.upsert_rule(r)
        from config import save_config
        save_config(self.config)
        await self.db.add_migration({
            "filename": os.path.basename(path),
            "source_node": pkg["manifest"].get("source_node_id"),
            "mode": mode, "status": "done",
            "log": json.dumps({"schema_version":
                               pkg["manifest"].get("schema_version")})})
        return {"status": "done", "mode": mode}

    async def clone(self, template_path: str) -> dict:
        backups_dir = os.path.realpath(os.path.join(self.data_dir, "backups"))
        real = os.path.realpath(template_path)
        # Template must live inside data_dir/backups — never an arbitrary
        # local path (a clone of /etc/passwd would be a file-oracle).
        if not real.startswith(backups_dir + os.sep) or not os.path.isfile(real):
            return {"status": "error",
                    "message": "template must be inside backups"}
        return await self.import_package(real, "merge")
