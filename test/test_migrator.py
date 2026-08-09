import asyncio
import io
import json
import os
import tarfile
import tempfile
import unittest

from db import Database
from migrator import MAX_PACKAGE_MEMBER_SIZE, Migrator, WorkerInterface


class MigratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-mig-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"port": 4790, "roots": ["/a"]}, f)
        self.db = Database(os.path.join(self.data, "webpty.db"))
        self.db.connect()
        self.config = {"port": 4790, "roots": ["/a"]}
        self.m = Migrator(self.data, self.config, self.db)

    def tearDown(self):
        self.db.close()

    async def test_worker_interface_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            WorkerInterface().export_state()

    async def test_export_creates_package(self):
        path = await self.m.export()
        self.assertTrue(os.path.exists(path))
        self.assertIn("webpty-migrate-", os.path.basename(path))
        self.assertGreater(os.path.getsize(path), 0)

    async def test_export_registers_in_backups(self):
        """export 生成的包登记进 backups 表(rotate 可清理,防孤儿文件)。"""
        path = await self.m.export()
        rows = await self.db.list_backups()
        self.assertTrue(any(r["filename"] == os.path.basename(path)
                            for r in rows))

    async def test_import_merge_preserves_existing(self):
        path = await self.m.export()
        # 修改现有配置制造冲突
        self.config["port"] = 9999
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["mode"], "merge")
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.config["port"], 4790)  # 包内值覆盖
        self.assertEqual(self.config["extra_key"], "mine")  # 现有键保留

    async def test_import_dry_run_does_not_write(self):
        path = await self.m.export()
        before = dict(self.config)
        res = await self.m.import_package(path, "dry-run")
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(self.config, before)
        self.assertIsInstance(res["changes"], list)
        # 每项只含 key/incoming——绝不回显当前配置值
        for c in res["changes"]:
            self.assertEqual(set(c.keys()), {"key", "incoming"})

    async def test_import_dry_run_redacts_secrets(self):
        self.config["authToken"] = "local-token"
        self.config["allowedLogins"] = ["a@x.com"]
        self.config["notify"] = {"smtp": {"password": "local-smtp-pass"}}
        path = await self.m.export()
        self.config["port"] = 4800  # export 后改动 → 与包内值不同
        res = await self.m.import_package(path, "dry-run")
        self.assertEqual(res["status"], "dry-run")
        # 响应中绝不出现任何当前 secret 值
        blob = json.dumps(res)
        self.assertNotIn("local-token", blob)
        self.assertNotIn("local-smtp-pass", blob)
        by_key = {c["key"]: c for c in res["changes"]}
        # 敏感键被 sanitize 完全拒绝导入:不出现在变更列表(比 redacted 更安全)
        for k in ("authToken", "allowedLogins"):
            self.assertNotIn(k, by_key, f"{k} must never be importable")
        # 嵌套敏感值(notify.smtp.password)导出时已脱敏为空
        self.assertEqual(by_key["notify"]["incoming"]["smtp"]["password"], "")
        # 非敏感键:展示 incoming 值
        self.assertEqual(by_key["port"]["incoming"], 4790)

    async def test_export_redacts_secrets(self):
        self.config["authToken"] = "super-secret-token"
        self.config["allowedLogins"] = ["admin@x.com"]
        self.config["notify"] = {"smtp": {"password": "smtp-secret",
                                          "user": "me@x.com"}}
        self.config["backup"] = {"encryption_key": "key-secret"}
        path = await self.m.export()
        with tarfile.open(path, "r:gz") as tf:
            state = json.loads(tf.extractfile("state.json").read())
            manifest = json.loads(tf.extractfile("manifest.json").read())
        cfg = state["config"]
        self.assertEqual(cfg["authToken"], "")
        self.assertEqual(cfg["allowedLogins"], "")
        self.assertEqual(cfg["notify"]["smtp"]["password"], "")
        self.assertEqual(cfg["backup"]["encryption_key"], "")
        # 非敏感内容原样保留
        self.assertEqual(cfg["notify"]["smtp"]["user"], "me@x.com")
        self.assertEqual(cfg["port"], 4790)
        self.assertIsNot(state["config"], self.config)  # 导出用副本
        self.assertTrue(manifest.get("secrets_redacted"))
        # 内存 config 未被脱敏污染
        self.assertEqual(self.config["authToken"], "super-secret-token")
        self.assertEqual(self.config["notify"]["smtp"]["password"],
                         "smtp-secret")

    async def test_import_restores_notify_rules(self):
        await self.db.upsert_rule({"name": "r1", "event_type": "failed",
                                   "matcher_json": "{}", "action": "email",
                                   "level": "warn"})
        path = await self.m.export()
        await self.db.delete_rule(1)  # 目标端规则已被删除
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["status"], "done")
        rules = await self.db.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "r1")
        self.assertEqual(rules[0]["event_type"], "failed")

    async def test_import_rules_overwrite_same_id(self):
        await self.db.upsert_rule({"name": "r1", "event_type": "failed",
                                   "matcher_json": "{}", "action": "email",
                                   "level": "warn"})
        path = await self.m.export()
        # 本地把同 id 规则改成 critical
        await self.db.upsert_rule({"id": 1, "name": "r1",
                                   "event_type": "failed",
                                   "matcher_json": "{}", "action": "email",
                                   "level": "critical"})
        await self.m.import_package(path, "merge")
        rules = await self.db.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["level"], "warn")  # 包内值覆盖

    async def _package_with_member(self, member: str, size: int) -> str:
        """Build a minimal valid package whose chosen member is `size` bytes."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in (("manifest.json",
                                json.dumps({"schema_version": 1}).encode()),
                               ("state.json", b'{"config": {}}')):
                if name == member:
                    data = b"x" * size
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        path = os.path.join(self.tmp, "huge.tar.gz")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        return path

    async def test_import_rejects_huge_state_member(self):
        path = await self._package_with_member(
            "state.json", MAX_PACKAGE_MEMBER_SIZE + 1)
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res, {"status": "error",
                               "message": "invalid package", "mode": "merge"})

    async def test_import_rejects_huge_manifest_member(self):
        path = await self._package_with_member(
            "manifest.json", MAX_PACKAGE_MEMBER_SIZE + 1)
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["status"], "error")

    async def test_import_rejects_corrupt_package(self):
        path = os.path.join(self.tmp, "garbage.tar.gz")
        with open(path, "wb") as f:
            f.write(b"not a tar")
        res = await self.m.import_package(path, "merge")
        self.assertEqual(res["status"], "error")

    async def test_clone_rejects_path_outside_backups(self):
        outside = os.path.join(self.tmp, "outside.tar.gz")
        with open(outside, "wb") as f:
            f.write(b"x")
        res = await self.m.clone(outside)
        self.assertEqual(res["status"], "error")
        self.assertIn("inside backups", res["message"])

    async def test_clone_accepts_export_inside_backups(self):
        path = await self.m.export()
        res = await self.m.clone(path)
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.config["port"], 4790)

    async def test_import_replace_overwrites(self):
        path = await self.m.export()
        self.config["extra_key"] = "mine"
        res = await self.m.import_package(path, "replace")
        self.assertNotIn("extra_key", self.config)

    async def test_source_node_id_stable(self):
        a = self.m.source_node_id()
        b = self.m.source_node_id()
        self.assertEqual(a, b)
        self.assertTrue(len(a) > 8)


    async def test_sanitize_blocks_rce_and_credentials(self):
        """导入恶意包:非内置 command 工具被丢弃、authToken 被拒。"""
        from migrator import sanitize_import_config
        evil = {
            "authToken": "attacker-token",
            "tools": {
                "good": {"command": "bash", "defaultArgs": "-c"},
                "evil": {"command": "/bin/sh", "defaultArgs": "-c id"},
            },
            "providers": {"deepseek": {"baseUrl": "https://x", "apiKey": "sk-evil"}},
            "port": 9999,
        }
        clean = sanitize_import_config(evil)
        self.assertNotIn("authToken", clean)
        self.assertIn("good", clean["tools"])
        self.assertNotIn("evil", clean["tools"])
        self.assertNotIn("apiKey", clean["providers"]["deepseek"])
        self.assertEqual(clean["port"], 9999)

    async def test_import_never_restores_sessions(self):
        """导入配置中的 sessions 键必须被剔除(运行时状态,防幽灵会话)。"""
        from migrator import sanitize_import_config
        clean = sanitize_import_config({
            "port": 4790, "sessions": [{"id": "ghost"}], "tools": {}})
        self.assertNotIn("sessions", clean)
        self.assertEqual(clean["port"], 4790)


if __name__ == "__main__":
    unittest.main()

