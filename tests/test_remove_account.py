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
import subprocess
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
            ok, out = self._run(workbuddy.remove_profile, "alice")

        self.assertFalse(ok, "非交互环境不许删当前账号")
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("非交互", out)

    def test_active_account_refused_when_only_yes(self):
        """脚本加 -y 不等于"同意删当前账号"，必须再显式 --force-active。"""
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        with patch.object(sys.stdin, "isatty", return_value=False):
            ok, out = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))
        self.assertIn("已拒绝", out)

    def test_active_account_detected_via_legacy_userid_field(self):
        """老数据把 UID 写在 account.userId 里，也必须认出它是当前账号。"""
        legacy = {"account": {"userId": "uid_legacy", "nickname": "老数据"}, "auth": {"accessToken": "t"}}
        with open(os.path.join(workbuddy.PROFILES_DIR, "legacy.info"), "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        os.makedirs(os.path.dirname(workbuddy.AUTH_FILE), exist_ok=True)
        with open(workbuddy.AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(legacy, f)

        with patch.object(sys.stdin, "isatty", return_value=False):
            ok, out = self._run(workbuddy.remove_profile, "legacy", yes=True)

        self.assertFalse(ok, "userId 形态的当前账号也不能被 -y 静默删掉")
        self.assertTrue(self._profile_exists("legacy"))

    def test_active_account_detected_via_shared_uin(self):
        """一边只有 uin、一边有 uid + uin 时，靠共有的 uin 也要认出是同一个账号。"""
        self._save_profile("alice", "uid_alice", nickname="小A")
        # profile 里补上 uin
        path = os.path.join(workbuddy.PROFILES_DIR, "alice.info")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["account"]["uin"] = "998877"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        # 登录态只有 uin
        os.makedirs(os.path.dirname(workbuddy.AUTH_FILE), exist_ok=True)
        with open(workbuddy.AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"account": {"uin": "998877", "nickname": "小A"}, "auth": {"accessToken": "t"}}, f)

        with patch.object(sys.stdin, "isatty", return_value=False):
            ok, _ = self._run(workbuddy.remove_profile, "alice", yes=True)

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))

    def test_survives_missing_stdin(self):
        """stdin 为 None（无控制台句柄）时不能抛 traceback，按非交互拒绝处理。"""
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_alice")

        with patch.object(sys, "stdin", None):
            ok, _ = self._run(workbuddy.remove_profile, "alice")

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("alice"))

    # ---------- 同一账号多个别名 ----------

    def test_uid_query_refused_when_multiple_aliases(self):
        """按 UID 删但该 UID 有两个别名时，只删一个等于没删 —— 必须拒绝并让人按别名删。"""
        self._save_profile("dup1", "uid_dup")
        self._save_profile("dup2", "uid_dup")
        self._save_profile("other", "uid_other")
        self._set_active("uid_other")

        ok, out = self._run(workbuddy.remove_profile, "uid_dup", yes=True)

        self.assertFalse(ok)
        self.assertTrue(self._profile_exists("dup1"))
        self.assertTrue(self._profile_exists("dup2"))
        self.assertIn("别名", out)

    def test_alias_delete_warns_about_twin_alias(self):
        self._save_profile("dup1", "uid_dup")
        self._save_profile("dup2", "uid_dup")
        self._set_active("uid_other")

        ok, out = self._run(workbuddy.remove_profile, "dup1", yes=True)

        self.assertTrue(ok)
        self.assertFalse(self._profile_exists("dup1"))
        self.assertTrue(self._profile_exists("dup2"))
        self.assertIn("别的别名", out)

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


    def test_purge_missing_file_is_not_reported_as_deleted(self):
        self._save_profile("alice", "uid_alice")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")
        os.remove(os.path.join(workbuddy.PROFILES_DIR, "alice.info"))

        ok, _ = self._run(workbuddy.remove_profile, "alice", yes=True, purge=True)

        self.assertFalse(ok, "文件本来就不在，不该报成功")

    def test_alias_named_help_can_still_be_removed(self):
        self._save_profile("help", "uid_help")
        self._save_profile("bob", "uid_bob")
        self._set_active("uid_bob")

        code, _ = self._run(workbuddy.run_remove_cmd, ["help", "-y"])

        self.assertEqual(code, 0)
        self.assertFalse(self._profile_exists("help"), "叫 help 的账号也要能删")

    def test_help_keyword_still_works_without_such_alias(self):
        self._save_profile("alice", "uid_alice")

        code, out = self._run(workbuddy.run_remove_cmd, ["help"])

        self.assertEqual(code, 0)
        self.assertIn("用法", out)
        self.assertTrue(self._profile_exists("alice"))

    def test_switch_still_works_after_removal(self):
        """验收 B：删完号，剩下的账号还得能正常切。"""
        self._save_profile("alice", "uid_alice")
        self._save_profile("keep", "uid_keep")
        self._set_active("uid_alice")

        workbuddy.remove_profile("alice", yes=True, force_active=True)

        with patch.object(workbuddy, "restart_workbuddy", lambda *a, **k: None):
            ok = workbuddy.switch_to_profile("keep", auto_restart=False, check_health=False)

        self.assertTrue(ok)
        with open(workbuddy.AUTH_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["account"]["uid"], "uid_keep")

    # ---------- 真实进程：main() 分发 ----------

    def _make_home_layout(self):
        """按子进程自己解析路径的方式铺一份 home（~/.workbuddy 下）。"""
        home = os.path.join(self.tmp, "home")
        profiles = os.path.join(home, ".workbuddy", "auth_profiles")
        auth_dir = os.path.join(home, ".workbuddy", "auth")
        os.makedirs(profiles, exist_ok=True)
        os.makedirs(auth_dir, exist_ok=True)
        for alias, uid in (("alice", "uid_alice"), ("bob", "uid_bob")):
            with open(os.path.join(profiles, f"{alias}.info"), "w", encoding="utf-8") as f:
                json.dump({"account": {"uid": uid, "nickname": alias}, "auth": {"accessToken": "t"}}, f)
        with open(os.path.join(auth_dir, "workbuddy-desktop.info"), "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": "uid_bob", "nickname": "bob"}, "auth": {"accessToken": "t"}}, f)
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": os.path.join(home, "appdata"),
            "LOCALAPPDATA": os.path.join(home, "localappdata"),
            "XDG_CONFIG_HOME": os.path.join(home, "xdg"),
            "NO_COLOR": "1",
        })
        return home, profiles, env

    def test_main_dispatch_removes_account(self):
        home, profiles, env = self._make_home_layout()
        script = os.path.join(REPO_DIR, "bin", "workbuddy")

        proc = subprocess.run([sys.executable, script, "rm", "alice", "-y"],
                              capture_output=True, text=True, env=env, timeout=120)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(profiles, "alice.info")))
        self.assertTrue(os.path.exists(os.path.join(profiles, "bob.info")))
        trash = os.path.join(profiles, ".trash")
        self.assertEqual(len(os.listdir(trash)), 1)

    def test_main_dispatch_blocks_active_account(self):
        home, profiles, env = self._make_home_layout()
        script = os.path.join(REPO_DIR, "bin", "workbuddy")

        proc = subprocess.run([sys.executable, script, "rm", "bob", "-y"],
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(profiles, "bob.info")))

    def test_prog_name_alias_dispatch(self):
        """wb-rm 这个入口文件名的分发要真的生效（install.py 装的就是这个别名）。"""
        home, profiles, env = self._make_home_layout()
        alias_script = os.path.join(home, "wb-rm")
        shutil.copy(os.path.join(REPO_DIR, "bin", "workbuddy"), alias_script)

        proc = subprocess.run([sys.executable, alias_script, "alice", "-y"],
                              capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(profiles, "alice.info")))

        # 多敲一次 "rm" 也要认（wb-rm rm bob 这种手滑）
        proc = subprocess.run([sys.executable, alias_script, "rm", "bob", "-y", "--force-active"],
                              capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(profiles, "bob.info")))

        proc = subprocess.run([sys.executable, alias_script, "--list-trash"],
                              capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("回收站", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
