"""Automatic configuration backups: snapshot → tar.gz + manifest + SHA256,
optional AES-GCM encryption (only when `cryptography` is importable),
retention rotation. Business-management layer.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time

from db import Database


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "created_at": time.time(),
        "content": ["config.json", "notify_rules", "sessions"],
        "sha256": "",
        "size_bytes": 0,
    }


async def collect_state(data_dir: str, config: dict, db: Database) -> dict:
    rules = await db.list_rules()
    cfg_path = os.path.join(data_dir, "config.json")
    sessions = []
    try:
        with open(cfg_path, encoding="utf-8") as f:
            stored = json.load(f)
        sessions = stored.get("sessions", []) if isinstance(stored, dict) else []
    except (OSError, json.JSONDecodeError):
        sessions = []
    return {"config": config, "notify_rules": rules, "sessions": sessions,
            "prices": config.get("prices", {})}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _maybe_encrypt(data: bytes, config: dict) -> tuple[bytes, bool]:
    key = (config.get("backup") or {}).get("encryption_key") or ""
    if not key:
        return data, False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return data, False
    nonce = os.urandom(12)
    ct = AESGCM(key.encode()[:32].ljust(32, b"\0")).encrypt(
        nonce, data, None)
    return nonce + ct, True


async def create_backup_async(data_dir: str, config: dict, db: Database) -> dict:
    backups_dir = os.path.join(data_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    state = await collect_state(data_dir, config, db)
    manifest = _manifest()
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    payload, encrypted = _maybe_encrypt(raw, config)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = payload if encrypted else raw
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    filename = (f"webpty-{time.strftime('%Y%m%d-%H%M%S')}"
                f"-{time.time_ns() % 1000000:06d}.tar.gz")
    path = os.path.join(backups_dir, filename)
    with open(path, "wb") as f:
        f.write(blob)
    sha = hashlib.sha256(blob).hexdigest()
    bid = await db.add_backup({
        "filename": filename, "size_bytes": len(blob), "sha256": sha,
        "manifest_json": json.dumps(manifest, sort_keys=True),
        "encrypted": 1 if encrypted else 0, "retained": 1})
    manifest["sha256"] = sha
    manifest["size_bytes"] = len(blob)
    return {"id": bid, "filename": filename, "sha256": sha,
            "size_bytes": len(blob), "encrypted": encrypted}


async def list_backups(db: Database) -> list[dict]:
    return await db.list_backups()


async def restore_backup(backup_id: int, data_dir: str, db: Database,
                         config: dict | None = None) -> dict:
    row = await db.get_backup(backup_id)
    if not row:
        return {"ok": False, "message": "backup not found"}
    path = os.path.join(data_dir, "backups", row["filename"])
    if not os.path.exists(path):
        return {"ok": False, "message": "file missing"}
    if _sha256_file(path) != row["sha256"]:
        return {"ok": False, "message": "sha256 mismatch"}
    with tarfile.open(path, "r:gz") as tf:
        member = tf.getmember("manifest.json")
        raw = tf.extractfile(member).read()
    if row.get("encrypted"):
        key = (config or {}).get("backup", {}).get("encryption_key") or ""
        if not key:
            return {"ok": False, "message": "backup is encrypted, key missing"}
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return {"ok": False, "message": "cryptography not installed"}
        nonce, ct = raw[:12], raw[12:]
        try:
            raw = AESGCM(key.encode()[:32].ljust(32, b"\0")).decrypt(
                nonce, ct, None)
        except InvalidTag:
            return {"ok": False,
                    "message": "decrypt failed (wrong key or corrupt)"}
    try:
        state = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "message": "corrupt manifest"}
    cfg = state.get("config") or {}
    cfg_path = os.path.join(data_dir, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}
    merged = dict(existing)
    merged.update(cfg)  # merge 语义：备份覆盖冲突键，保留现有新增键
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    # 恢复通知规则:同 id 覆盖,其余新增(sessions 是运行时状态,不恢复)
    existing_ids = {r["id"] for r in await db.list_rules()}
    for rule in state.get("notify_rules") or []:
        if not isinstance(rule, dict) or not rule.get("name") \
                or not rule.get("event_type"):
            continue
        r = dict(rule)
        if r.get("id") not in existing_ids:
            r.pop("id", None)
        await db.upsert_rule(r)
    # 返回合并后的完整 config,调用方负责同步内存
    return {"ok": True, "message": "restored", "config": merged}


async def diff_backups(a_id: int, b_id: int, db: Database) -> list[dict]:
    async def _load(bid: int) -> dict:
        """Read the state dict from a backup package by id."""
        row = await db.get_backup(bid)
        if not row:
            return {}
        path = os.path.join(os.path.dirname(db.path), "backups",
                            os.path.basename(row["filename"]))
        if not os.path.exists(path):
            return {}
        with tarfile.open(path, "r:gz") as tf:
            return json.loads(tf.extractfile("manifest.json").read())

    a = await db.get_backup(a_id)
    b = await db.get_backup(b_id)
    if not a or not b:
        return [{"key": "_error", "a": "missing", "b": "missing"}]
    sa = await _load(a_id)
    sb = await _load(b_id)
    ca = sa.get("config") or {}
    cb = sb.get("config") or {}
    keys = set(ca.keys()) | set(cb.keys())
    return [{"key": k, "a": ca.get(k), "b": cb.get(k)}
            for k in sorted(keys) if ca.get(k) != cb.get(k)]


async def rotate(db: Database, retention: int = 7) -> list[int]:
    rows = await db.list_backups()
    if len(rows) <= retention:
        return []
    doomed = rows[retention:]
    deleted = []
    for row in doomed:
        path = os.path.join(os.path.dirname(db.path), "backups",
                            os.path.basename(row["filename"]))
        if os.path.exists(path):
            os.remove(path)
        await db.delete_backup(row["id"])
        deleted.append(row["id"])
    return deleted
