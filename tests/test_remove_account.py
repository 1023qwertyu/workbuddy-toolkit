#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
"删账号不会把本地搞坏"的回归测试。

覆盖的真实故障:
- 删掉正在使用的账号：本地没有凭证备份，想再用只能重新扫码；
- 直接物理删除不可恢复，手滑一次就得重新登录；
- 回收站里的文件又被 get_profiles() 扫成"账号"，删了等于没删；
- 删账号顺手动了本地库里别的账号的会话/自动化归属。
"""

import io
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import importlib.util
import importlib.machinery

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("workbuddy_rm", os.path.join(REPO_DIR, "bin", "workbuddy"))
_spec = importlib.util.spec_from_loader("workbuddy_rm", _loader)
workbuddy = importlib.util.module_from_spec(_spec)
_loader.exec_module(workbuddy)


class _SandboxMixin:
    def _setup_sandbox(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (workbuddy.AUTH_FILE, workbuddy.PROFILES_DIR, workbuddy.DB_FILE, workbuddy.AUDIT_CACHE_FILE)
        workbuddy.AUTH_FILE = os.path.join(self.tmp, "auth", "workbuddy-desktop.info")
        workbuddy.PROFILES_DIR = os.path.join(self.tmp, "auth_profiles")
        workbuddy.DB_FILE = os.path.join(self.tmp, "workbuddy.db")
        workbuddy.AUDIT_CACHE_FILE = os.path.join(self.tmp, "cache", "audit_cache.json")
        os.makedirs(os.path.dirname(workbuddy.AUTH_FILE))
        os.makedirs(workbuddy.PROFILES_DIR)
        con = sqlite3.connect(workbuddy.DB_FILE)
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT)")
        con.execute("CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_user_id TEXT, deleted_at INTEGER)")
        con.commit()
        con.close()

    def _teardown_sandbox(self):
        (workbuddy.AUTH_FILE, workbuddy.PROFILES_DIR, workbuddy.DB_FILE, workbuddy.AUDIT_CACHE_FILE) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self):
        return sqlite3.connect(workbuddy.DB_FILE)

    def _save_profile(self, alias, uid, nickname=None, token=None):
        data = {
            "account": {"uid": uid, "nickname": nickname or alias},
            "auth": {"accessToken": token or f"tok_{alias}"},
        }
        path = os.path.join(workbuddy.PROFILES_DIR, f"{alias}.info")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    def _set_active(self, uid, nickname="active"):
        data = {"account": {"uid": uid, "nickname": nickname}, "auth": {"accessToken": f"tok_active_{uid}"}}
        os.makedirs(os.path.dirname(workbuddy.AUTH_FILE), exist_ok=True)
        with open(workbuddy.AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    def _profile_exists(self, alias):
        return os.path.exists(os.path.join(workbuddy.PROFILES_DIR, f"{alias}.info"))

    def _trash_files(self):
        d = os.path.join(workbuddy.PROFILES_DIR, ".trash")
        if not os.path.isdir(d):
            return []
        return sorted(os.listdir(d))

    def _run(self, func, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = func(*args, **kwargs)
        return result, buf.getvalue()


class TestRemoveAccount(_SandboxMixin, unittest.TestCase):
    def setUp(self):
        self._setup_sandbox()

    def tearDown(self):
        self._teardown_sandbox()

    # ---------- 正常删除 ----------

    def test_remove_moves_to_trash(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        ok, out = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("alice"), "原文件应已移走")
        self.assertEqual(len(self._trash_files()), 1, "应有一个文件进回收站")
        self.assertTrue(self._trash_files()[0].endswith(".info"))
        self.assertTrue(self._profile_exists("bob"), "别的账号不能被连带删掉")
        self.assertIn("回收站", out)

    def test_trash_entries_not_rescanned_as_profiles(self):
        """回收站里的文件不能被 get_profiles() 重新扫出来 —— 否则删了等于没删。"""
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        workbuddy.remove_profile("alice", yes=True)

        self.assertEqual(sorted(workbuddy.get_profiles().keys()), ["bob"])

    def test_remove_by_nickname(self):
        self._save_profile("a1", "uid_alice", nickname="老王")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        ok, _ = self._run(workbuddy.remove_profile, "老王", yes=True)

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("a1"))

    def test_remove_unknown_target_changes_nothing(self):
        self._save_profile("alice", "uid_alice")
        self._set_active("uid_alice")

        ok, out = self._run(workbuddy.remove_profile, "ghost", yes=True)

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertEqual(self._trash_files(), [])
        self.assertIn("未找到", out)

    # ---------- 活跃账号保护 ----------

    def test_active_account_blocked_when_not_interactive(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        with patch.object(sys.stdin, "isatty", return_value=False):
            ok, out = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertFalse(ok, "非交互环境不许删当前账号")
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("非交互", out)

    def test_active_account_needs_force_active_with_yes(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        ok, _ = self._run(workbuddy.remove_profile, "alice", yes=True, force_active=True)

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("alice"))

    def test_active_account_declined_interactively(self):
        self._save_profile("alice", "uid_alice")
        self._set_active("uid_alice")

        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="n"):
            ok, out = self._run(workbuddy.remove_profile, "alice")

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("已取消", out)

    def test_active_account_confirmed_interactively(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="yes"):
            ok, _ = self._run(workbuddy.remove_profile, "alice")

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("alice"))
        self.assertEqual(len(self._trash_files()), 1)

    def test_normal_account_declined_interactively(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="n"):
            ok, out = self._run(workbuddy.remove_profile, "alice")

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("已取消", out)

    # ---------- 永久删除 / 预览 ----------

    def test_purge_deletes_without_trash(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        ok, out = self._run(workbuddy.remove_profile, "alice", yes=True, purge=True)

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("alice"))
        self.assertEqual(self._trash_files(), [], "purge 不应留回收站文件")
        self.assertIn("永久删除", out)

    def test_dry_run_touches_nothing(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        ok, out = self._run(workbuddy.remove_profile, "alice", dry_run=True)

        self.assertTrue(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertEqual(self._trash_files(), [])
        self.assertIn("dry-run", out)

    # ---------- 回收站 / 恢复 ----------

    def test_restore_roundtrip_keeps_content(self):
        original = self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        workbuddy.remove_profile("alice", yes=True)
        ok, _ = self._run(workbuddy.restore_profile, "alice")

        self.assertTrue(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertEqual(self._trash_files(), [])
        with open(os.path.join(workbuddy.PROFILES_DIR, "alice.info"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), original, "恢复后内容要一模一样")

    def test_restore_refuses_to_overwrite_existing(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        workbuddy.remove_profile("alice", yes=True)
        self._save_profile("alice", "uid_other", nickname="新人")

        ok, out = self._run(workbuddy.restore_profile, "alice")

        self.assertFalse(ok, "同名账号已存在时不许覆盖")
        with open(os.path.join(workbuddy.PROFILES_DIR, "alice.info"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["account"]["uid"], "uid_other")
        self.assertEqual(len(self._trash_files()), 1, "回收站里的备份要留着")
        self.assertIn("已经存在", out)

    def test_restore_unknown_alias(self):
        ok, out = self._run(workbuddy.restore_profile, "nobody")
        self.assertFalse(ok)
        self.assertIn("找不到", out)

    def test_empty_trash(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        workbuddy.remove_profile("alice", yes=True)

        ok, _ = self._run(workbuddy.empty_trash, yes=True)

        self.assertTrue(ok)
        self.assertEqual(self._trash_files(), [])

    def test_empty_trash_declined(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        workbuddy.remove_profile("alice", yes=True)

        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="n"):
            ok, _ = self._run(workbuddy.empty_trash)

        self.assertFalse(ok)
        self.assertEqual(len(self._trash_files()), 1)

    # ---------- 不碰别人的历史 ----------

    def test_audit_cache_entry_cleared_only_for_target(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        os.makedirs(os.path.dirname(workbuddy.AUDIT_CACHE_FILE), exist_ok=True)
        with open(workbuddy.AUDIT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"uid_alice": {"status": "HEALTHY"}, "uid_bob": {"status": "HEALTHY"}}, f)

        workbuddy.remove_profile("alice", yes=True)

        with open(workbuddy.AUDIT_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        self.assertNotIn("uid_alice", cache)
        self.assertIn("uid_bob", cache, "别的账号的缓存不许动")

    def test_ownership_tables_untouched(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        con = self._db()
        con.execute(f"CREATE TABLE {workbuddy.SESSION_OWNER_BACKUP_TABLE} "
                    f"(session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, saved_at INTEGER NOT NULL)")
        con.execute(f"INSERT INTO {workbuddy.SESSION_OWNER_BACKUP_TABLE} VALUES ('s1', 'uid_alice', 1)")
        con.execute(f"CREATE TABLE {workbuddy.AUTOMATION_OWNER_BACKUP_TABLE} "
                    f"(automation_id TEXT PRIMARY KEY, owner_user_id TEXT, saved_at INTEGER NOT NULL)")
        con.execute(f"INSERT INTO {workbuddy.AUTOMATION_OWNER_BACKUP_TABLE} VALUES ('a1', 'uid_alice', 1)")
        con.execute("INSERT INTO sessions VALUES ('s1', '/tmp', 'uid_bob', 't')")
        con.commit()
        con.close()

        ok, out = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertTrue(ok)
        con = self._db()
        self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {workbuddy.SESSION_OWNER_BACKUP_TABLE}").fetchone()[0], 1)
        self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {workbuddy.AUTOMATION_OWNER_BACKUP_TABLE}").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT user_id FROM sessions WHERE id='s1'").fetchone()[0], "uid_bob")
        con.close()
        self.assertIn("归属", out)

    def test_ownership_count_reads_rows(self):
        con = self._db()
        con.execute(f"CREATE TABLE {workbuddy.SESSION_OWNER_BACKUP_TABLE} "
                    f"(session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, saved_at INTEGER NOT NULL)")
        con.execute(f"INSERT INTO {workbuddy.SESSION_OWNER_BACKUP_TABLE} VALUES ('s1', 'uid_alice', 1)")
        con.execute(f"INSERT INTO {workbuddy.SESSION_OWNER_BACKUP_TABLE} VALUES ('s2', 'uid_bob', 1)")
        con.execute(f"CREATE TABLE {workbuddy.AUTOMATION_OWNER_BACKUP_TABLE} "
                    f"(automation_id TEXT PRIMARY KEY, owner_user_id TEXT, saved_at INTEGER NOT NULL)")
        con.execute(f"INSERT INTO {workbuddy.AUTOMATION_OWNER_BACKUP_TABLE} VALUES ('a1', 'uid_alice', 1)")
        con.commit()
        con.close()

        counts = workbuddy._count_ownership_rows("uid_alice")

        self.assertEqual(counts[workbuddy.SESSION_OWNER_BACKUP_TABLE], 1)
        self.assertEqual(counts[workbuddy.AUTOMATION_OWNER_BACKUP_TABLE], 1)

    def test_ownership_count_with_space_in_path(self):
        """库路径里有空格 / 中文时，只读 URI 也要能连上，否则统计会静默变成"没有记录"。"""
        spaced_dir = os.path.join(self.tmp, "带 空格 的目录")
        os.makedirs(spaced_dir, exist_ok=True)
        spaced_db = os.path.join(spaced_dir, "workbuddy.db")
        con = sqlite3.connect(spaced_db)
        con.execute(f"CREATE TABLE {workbuddy.SESSION_OWNER_BACKUP_TABLE} "
                    f"(session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, saved_at INTEGER NOT NULL)")
        con.execute(f"INSERT INTO {workbuddy.SESSION_OWNER_BACKUP_TABLE} VALUES ('s1', 'uid_alice', 1)")
        con.commit()
        con.close()

        original_db = workbuddy.DB_FILE
        workbuddy.DB_FILE = spaced_db
        try:
            counts = workbuddy._count_ownership_rows("uid_alice")
        finally:
            workbuddy.DB_FILE = original_db

        self.assertEqual(counts[workbuddy.SESSION_OWNER_BACKUP_TABLE], 1)

    def test_ownership_count_without_db(self):
        workbuddy.DB_FILE = os.path.join(self.tmp, "no-such.db")
        self.assertEqual(workbuddy._count_ownership_rows("uid_alice"), {})

    def test_no_token_leaked_to_output(self):
        self._save_profile("alice", "uid_alice", token="super-secret-token")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        ok, out = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertTrue(ok, "删的是非活跃账号，应当成功")
        self.assertNotIn("super-secret-token", out)

    def test_active_auth_file_untouched(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        active = self._set_active("uid_alice")
        with open(workbuddy.AUTH_FILE, "r", encoding="utf-8") as f:
            before = json.load(f)

        workbuddy.remove_profile("alice", yes=True, force_active=True)

        with open(workbuddy.AUTH_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), before, "客户端的登录态不该被删号动作改写")
        self.assertEqual(before, active)


class TestRemoveCommandCli(_SandboxMixin, unittest.TestCase):
    def setUp(self):
        self._setup_sandbox()

    def tearDown(self):
        self._teardown_sandbox()

    def test_list_trash_exit_zero(self):
        code, out = self._run(workbuddy.run_remove_cmd, ["--list-trash"])
        self.assertEqual(code, 0)
        self.assertIn("回收站", out)

    def test_no_target_exit_one(self):
        code, out = self._run(workbuddy.run_remove_cmd, [])
        self.assertEqual(code, 1)
        self.assertIn("请指定", out)

    def test_unknown_option_rejected(self):
        code, out = self._run(workbuddy.run_remove_cmd, ["alice", "--purgee"])
        self.assertEqual(code, 1)
        self.assertIn("未知选项", out)

    def test_two_targets_rejected(self):
        code, out = self._run(workbuddy.run_remove_cmd, ["alice", "bob"])
        self.assertEqual(code, 1)
        self.assertIn("一次只能删一个", out)

    def test_dry_run_via_cli(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        code, out = self._run(workbuddy.run_remove_cmd, ["alice", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("dry-run", out)

    def test_yes_via_cli_removes(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        code, _ = self._run(workbuddy.run_remove_cmd, ["alice", "-y"])

        self.assertEqual(code, 0)
        self.assertFalse(self._profile_exists("alice"))

    def test_restore_via_cli(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        workbuddy.run_remove_cmd(["alice", "-y"])

        code, _ = self._run(workbuddy.run_remove_cmd, ["--restore", "alice"])

        self.assertEqual(code, 0)
        self.assertTrue(self._profile_exists("alice"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
