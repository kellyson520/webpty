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
        manifest = {
            "schema_version": 1,
            "created_at": time.time(),
            "source_node_id": self.source_node_id(),
            "content": ["config", "notify_rules", "sessions", "prices"],
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
        return path

    def _read_package(self, path: str) -> dict | None:
        """Read manifest.json + state.json from a package. Path-traversal safe:
        only fixed member names are read, nothing is extracted to disk."""
        try:
            with tarfile.open(path, "r:gz") as tf:
                man_raw = tf.extractfile("manifest.json")
                st_raw = tf.extractfile("state.json")
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
        if mode == "dry-run":
            current = dict(self.config)
            changed = {k: {"current": current.get(k), "incoming": v}
                       for k, v in incoming.items()
                       if current.get(k) != v}
            return {"status": "dry-run", "mode": mode, "changes": changed}
        if mode == "replace":
            self.config.clear()
            self.config.update(incoming)
        else:  # merge (default): package values win for keys it carries,
            # existing keys absent from the package are kept (no clear)
            self.config.update(incoming)
        with open(os.path.join(self.data_dir, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
        await self.db.add_migration({
            "filename": os.path.basename(path),
            "source_node": pkg["manifest"].get("source_node_id"),
            "mode": mode, "status": "done",
            "log": json.dumps({"schema_version":
                               pkg["manifest"].get("schema_version")})})
        return {"status": "done", "mode": mode}

    async def clone(self, template_path: str) -> dict:
        return await self.import_package(template_path, "merge")
