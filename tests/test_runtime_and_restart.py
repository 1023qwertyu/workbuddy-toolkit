#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行时探测、客户端重启与明文回退告警的回归测试。

覆盖的真实故障:
- Linux 上 `which workbuddy` 命中 Toolkit 自己的 CLI，被当成 Electron 运行时；
- Linux 上 `pkill -f "/workbuddy..."` 命中 `workbuddy switch` 自身进程（退出码 143）；
- 加密不可用时静默以明文保存凭据。
"""

import io
import os
import sys
import shutil
import tempfile
import unittest
import subprocess
import time
from unittest.mock import patch

import importlib.util
import importlib.machinery

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_PATH = os.path.join(REPO_DIR, "bin", "workbuddy")
_loader = importlib.machinery.SourceFileLoader("workbuddy_rt", BIN_PATH)
_spec = importlib.util.spec_from_loader("workbuddy_rt", _loader)
workbuddy = importlib.util.module_from_spec(_spec)
_loader.exec_module(workbuddy)


class TestRuntimeDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_cli = os.path.join(self.tmp, "workbuddy")
        shutil.copy2(BIN_PATH, self.fake_cli)
        os.chmod(self.fake_cli, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_toolkit_cli_is_recognized(self):
        self.assertTrue(workbuddy._is_toolkit_cli(BIN_PATH))
        self.assertTrue(workbuddy._is_toolkit_cli(self.fake_cli))
        self.assertFalse(workbuddy._is_toolkit_cli(sys.executable))

    def test_linux_which_hit_on_toolkit_cli_is_skipped(self):
        with patch.object(workbuddy.sys, "platform", "linux"), \
             patch.object(workbuddy, "LINUX_APP_CANDIDATES", []), \
             patch.object(workbuddy.shutil, "which", return_value=self.fake_cli), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKBUDDY_EXE", None)
            with self.assertRaises(workbuddy.CredentialError) as ctx:
                workbuddy.find_workbuddy_runtime()
            self.assertEqual(ctx.exception.reason, "RUNTIME_NOT_FOUND")

    def test_linux_fixed_install_path_preferred_over_path_lookup(self):
        real_app = os.path.join(self.tmp, "opt", "WorkBuddy", "workbuddy")
        os.makedirs(os.path.dirname(real_app))
        with open(real_app, "wb") as f:
            f.write(b"\x7fELF fake electron binary")
        os.chmod(real_app, 0o755)
        with patch.object(workbuddy.sys, "platform", "linux"), \
             patch.object(workbuddy, "LINUX_APP_CANDIDATES", [real_app]), \
             patch.object(workbuddy.shutil, "which", return_value=self.fake_cli), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKBUDDY_EXE", None)
            self.assertEqual(workbuddy.find_workbuddy_runtime(), os.path.abspath(real_app))

    def test_workbuddy_exe_override_pointing_at_toolkit_is_rejected(self):
        with patch.dict(os.environ, {"WORKBUDDY_EXE": self.fake_cli}):
            with self.assertRaises(workbuddy.CredentialError) as ctx:
                workbuddy.find_workbuddy_runtime()
            self.assertEqual(ctx.exception.reason, "INVALID_RUNTIME_PATH")


@unittest.skipUnless(sys.platform.startswith("linux"), "依赖 /proc")
class TestLinuxRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_fake_app(self):
        app = os.path.join(self.tmp, "opt", "WorkBuddy", "workbuddy")
        os.makedirs(os.path.dirname(app), exist_ok=True)
        shutil.copy2(shutil.which("sleep"), app)
        p = subprocess.Popen([app, "30"])
        self.procs.append(p)
        return app, p

    def _spawn_toolkit_like_process(self):
        # 命令行形如 `python3 .../workbuddy switch acc2`，正是旧版 pkill -f 会误杀的形态
        cli_dir = os.path.join(self.tmp, "bin")
        os.makedirs(cli_dir, exist_ok=True)
        cli = os.path.join(cli_dir, "workbuddy")
        with open(cli, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(30)\n")
        p = subprocess.Popen([sys.executable, cli, "switch", "acc2"])
        self.procs.append(p)
        return p

    def test_app_pids_match_binary_not_command_line(self):
        app, app_proc = self._spawn_fake_app()
        cli_proc = self._spawn_toolkit_like_process()
        time.sleep(0.2)
        pids = workbuddy._linux_app_pids(app)
        self.assertIn(app_proc.pid, pids)
        self.assertNotIn(cli_proc.pid, pids)
        self.assertNotIn(os.getpid(), pids)

    def test_restart_never_kills_itself_or_other_cli_processes(self):
        app, app_proc = self._spawn_fake_app()
        cli_proc = self._spawn_toolkit_like_process()
        time.sleep(0.2)
        commands = []
        real_run = subprocess.run

        def recording_run(cmd, *a, **kw):
            commands.append(cmd)
            return real_run(cmd, *a, **kw)

        with patch.object(workbuddy, "find_workbuddy_runtime", return_value=app), \
             patch.object(workbuddy, "_launch_detached") as launch, \
             patch.object(workbuddy.subprocess, "run", side_effect=recording_run), \
             patch("sys.stdout", new_callable=io.StringIO):
            ok = workbuddy.restart_workbuddy()

        self.assertTrue(ok)
        launch.assert_called_once_with(app)
        self.assertFalse(any("pkill" in str(c) for c in commands), "Linux 重启不得再使用 pkill 模糊匹配")
        app_proc.wait(timeout=6)
        self.assertIsNotNone(app_proc.poll(), "客户端进程应被终止")
        self.assertIsNone(cli_proc.poll(), "Toolkit 自身 / 其他 CLI 进程不得被误杀")

    def test_restart_reports_failure_when_app_missing(self):
        with patch.object(workbuddy, "find_workbuddy_runtime", side_effect=workbuddy.CredentialError("RUNTIME_NOT_FOUND")), \
             patch.object(workbuddy, "_launch_detached") as launch, \
             patch.object(workbuddy, "_linux_app_pids", return_value=[]), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            ok = workbuddy.restart_workbuddy()
        self.assertFalse(ok)
        launch.assert_not_called()
        self.assertNotIn("已成功重启", out.getvalue())


class TestPlaintextFallbackWarning(unittest.TestCase):
    def setUp(self):
        workbuddy._PLAINTEXT_FALLBACK_WARNED = False

    def test_warns_when_runtime_found_but_encryption_fails(self):
        with patch.object(workbuddy, "find_workbuddy_runtime", return_value="/mock/WorkBuddy"), \
             patch.object(workbuddy, "run_native_resolver", side_effect=workbuddy.CredentialError("RUNTIME_UNAVAILABLE")), \
             patch.object(workbuddy, "get_active_account", return_value=None), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            out = workbuddy.encrypt_credential_field("abc.def", "accessToken")
        self.assertEqual(out, "abc.def")
        self.assertIn("明文", err.getvalue())

    def test_warns_when_client_uses_encryption_but_runtime_missing(self):
        enc_active = {"auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "x"}}}
        with patch.object(workbuddy, "find_workbuddy_runtime", side_effect=workbuddy.CredentialError("RUNTIME_NOT_FOUND")), \
             patch.object(workbuddy, "get_active_account", return_value=enc_active), \
             patch.object(workbuddy, "check_token_format", return_value="sym-v1"), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            workbuddy.encrypt_credential_field("abc.def", "accessToken")
        self.assertIn("明文", err.getvalue())

    def test_silent_for_legacy_plaintext_clients(self):
        legacy_active = {"auth": {"accessToken": "legacy.plain.token"}}
        with patch.object(workbuddy, "find_workbuddy_runtime", side_effect=workbuddy.CredentialError("RUNTIME_NOT_FOUND")), \
             patch.object(workbuddy, "get_active_account", return_value=legacy_active), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            workbuddy.encrypt_credential_field("abc.def", "accessToken")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
