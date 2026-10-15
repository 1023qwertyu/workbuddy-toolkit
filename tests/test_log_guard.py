import os
import sys
import time
import shutil
import tempfile
import unittest
import importlib.util

import importlib.machinery

def load_log_guard():
    loader = importlib.machinery.SourceFileLoader("workbuddy_log_guard", os.path.join(os.path.dirname(__file__), "..", "bin", "workbuddy-log-guard"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

class TestLogGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["WB_LOG_DIR"] = self.temp_dir
        os.environ["WB_TRACES_DIR"] = os.path.join(self.temp_dir, "traces")
        self.guard = load_log_guard()
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if "WB_LOG_DIR" in os.environ:
            del os.environ["WB_LOG_DIR"]
        if "WB_TRACES_DIR" in os.environ:
            del os.environ["WB_TRACES_DIR"]

    def test_is_file_open_windows(self):
        original_name = os.name
        os.name = 'nt'
        
        filepath = os.path.join(self.temp_dir, "test.log")
        with open(filepath, "w") as f:
            f.write("test")
            
        original_rename = os.rename
        def mock_rename(src, dst):
            e = OSError("Sharing violation")
            e.winerror = 32
            raise e
            
        os.rename = mock_rename
        
        try:
            self.assertTrue(self.guard.is_file_open(filepath, set()))
        finally:
            os.rename = original_rename
            os.name = original_name

    def test_cleanup_locked_sandbox_file_windows(self):
        original_name = os.name
        os.name = 'nt'
        
        sandbox_dir = os.path.join(self.temp_dir, "sandbox", "old_date")
        os.makedirs(sandbox_dir)
        filepath = os.path.join(sandbox_dir, "locked.log")
        with open(filepath, "w") as f:
            f.write("A" * 1024)
            
        old_time = time.time() - (3600 * 48)
        os.utime(filepath, (old_time, old_time))
        
        original_is_file_open = self.guard.is_file_open
        self.guard.is_file_open = lambda fp, ofs: True
        
        try:
            self.guard.main()
            self.assertTrue(os.path.exists(filepath))
            # On Windows, we should skip truncation entirely if the file is locked
            self.assertEqual(os.path.getsize(filepath), 1024)
        finally:
            self.guard.is_file_open = original_is_file_open
            os.name = original_name

    def test_clean_runaway_temp_logs_truncates_oversized(self):
        temp_watch = os.path.join(self.temp_dir, "mock_tmp")
        os.makedirs(temp_watch, exist_ok=True)
        oversized_log = os.path.join(temp_watch, "t-fail.log")
        with open(oversized_log, "w") as f:
            f.write("FAIL " * 1000)
        file_size = os.path.getsize(oversized_log)

        normal_log = os.path.join(temp_watch, "small.log")
        with open(normal_log, "w") as f:
            f.write("OK\n")
        normal_size = os.path.getsize(normal_log)

        other_file = os.path.join(temp_watch, "large.data")
        with open(other_file, "w") as f:
            f.write("D" * 2000)

        # Max size 0.0005 MB (~500 bytes), so oversized_log (>4000 bytes) will be truncated
        # 失控日志的前提是仍有进程在写：测试进程持有一个追加句柄模拟测试死循环
        writer = open(oversized_log, "a")
        try:
            cleaned, freed = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
        finally:
            writer.close()
        self.assertEqual(cleaned, 1)
        self.assertGreaterEqual(freed, file_size)
        self.assertTrue(os.path.exists(oversized_log))
        self.assertEqual(os.path.getsize(oversized_log), 0)
        self.assertEqual(os.path.getsize(normal_log), normal_size)
        self.assertEqual(os.path.getsize(other_file), 2000)

    def test_clean_runaway_temp_logs_windows_semantics(self):
        """Windows: 没有进程持有的文件视为静止，跳过；被持有但写入方独占锁导致截断失败时，同样安全跳过。"""
        original_name = os.name
        os.name = "nt"
        temp_watch = os.path.join(self.temp_dir, "mock_tmp_win")
        os.makedirs(temp_watch, exist_ok=True)
        log = os.path.join(temp_watch, "runaway.log")
        with open(log, "w") as f:
            f.write("E" * 2000)

        original_is_file_open = self.guard.is_file_open
        original_truncate = self.guard.truncate_file
        try:
            # 1. 无人持有 -> 静止文件，不截断
            self.guard.is_file_open = lambda fp, ofs: False
            cleaned, freed = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
            self.assertEqual((cleaned, freed), (0, 0))
            self.assertEqual(os.path.getsize(log), 2000)

            # 2. 被持有但写入方独占锁 -> 截断失败，安全跳过
            self.guard.is_file_open = lambda fp, ofs: True
            self.guard.truncate_file = lambda fp: False
            cleaned, freed = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
            self.assertEqual((cleaned, freed), (0, 0))
            self.assertEqual(os.path.getsize(log), 2000)

            # 3. 被持有且可截断 -> 截断
            self.guard.truncate_file = original_truncate
            cleaned, _ = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
            self.assertEqual(cleaned, 1)
            self.assertEqual(os.path.getsize(log), 0)
        finally:
            self.guard.is_file_open = original_is_file_open
            self.guard.truncate_file = original_truncate
            os.name = original_name

    def test_clean_bundle_migration_cache_stale(self):
        bm_dir = os.path.join(self.temp_dir, "BundleMigration")
        backups = os.path.join(bm_dir, "backups")
        downloads = os.path.join(bm_dir, "downloads")
        os.makedirs(backups, exist_ok=True)
        os.makedirs(downloads, exist_ok=True)

        old_backup = os.path.join(backups, "old.backup.app")
        os.makedirs(old_backup, exist_ok=True)
        with open(os.path.join(old_backup, "dummy"), "w") as f:
            f.write("backup")

        new_backup = os.path.join(backups, "recent.backup.app")
        os.makedirs(new_backup, exist_ok=True)
        with open(os.path.join(new_backup, "dummy"), "w") as f:
            f.write("recent")

        old_zip = os.path.join(downloads, "update-old.zip")
        with open(old_zip, "w") as f:
            f.write("zipcontent")

        old_time = time.time() - (3600 * 96)  # 4 days ago
        os.utime(old_backup, (old_time, old_time))
        os.utime(old_zip, (old_time, old_time))

        cleaned, freed = self.guard.clean_bundle_migration_cache(bm_dir, ttl_hours=72)
        self.assertEqual(cleaned, 2)
        self.assertGreater(freed, 0)
        self.assertFalse(os.path.exists(old_backup))
        self.assertFalse(os.path.exists(old_zip))
        self.assertTrue(os.path.exists(new_backup))

    def test_main_integration_invokes_temp_log_and_bundle_guard(self):
        # Create a mock tmp directory and point WB_TMP_DIR to it
        mock_tmp = os.path.join(self.temp_dir, "mock_sys_tmp")
        os.makedirs(mock_tmp, exist_ok=True)
        os.environ["WB_TMP_DIR"] = mock_tmp
        os.environ["WB_MAX_TEMP_FILE_MB"] = "0.0005"  # ~500 bytes

        oversized_log = os.path.join(mock_tmp, "crash-runaway.log")
        with open(oversized_log, "w") as f:
            f.write("ERROR TRACEBACK\n" * 200)
        self.assertGreater(os.path.getsize(oversized_log), 1000)

        writer = open(oversized_log, "a")
        try:
            self.guard.main()
            self.assertTrue(os.path.exists(oversized_log))
            self.assertEqual(os.path.getsize(oversized_log), 0)
        finally:
            writer.close()
            if "WB_TMP_DIR" in os.environ:
                del os.environ["WB_TMP_DIR"]
            if "WB_MAX_TEMP_FILE_MB" in os.environ:
                del os.environ["WB_MAX_TEMP_FILE_MB"]


    def test_clean_runaway_temp_logs_never_touches_files_at_rest(self):
        """回归：大文件只因文件名含 -err/-crash 或后缀为 .txt/.out 就被清零（真实数据丢失）。"""
        if os.name == "nt":
            self.skipTest("POSIX 语义")
        temp_watch = os.path.join(self.temp_dir, "mock_tmp_user_data")
        os.makedirs(temp_watch, exist_ok=True)
        names = ["dataset-errata.tar.gz", "my-crash-dump.sqlite", "experiment_results.txt",
                 "build.out", "finished-run.log"]
        for n in names:
            with open(os.path.join(temp_watch, n), "w") as f:
                f.write("X" * 4000)
        cleaned, _ = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
        self.assertEqual(cleaned, 0, "没有进程在写的大文件不是失控日志，不得截断")
        for n in names:
            self.assertEqual(os.path.getsize(os.path.join(temp_watch, n)), 4000, n)

    def test_clean_runaway_temp_logs_ignores_keyword_without_log_suffix_even_if_open(self):
        if os.name == "nt":
            self.skipTest("POSIX 语义")
        temp_watch = os.path.join(self.temp_dir, "mock_tmp_open_archive")
        os.makedirs(temp_watch, exist_ok=True)
        archive = os.path.join(temp_watch, "nightly-crash-bundle.tar.gz")
        with open(archive, "w") as f:
            f.write("X" * 4000)
        holder = open(archive, "a")
        try:
            cleaned, _ = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
        finally:
            holder.close()
        self.assertEqual(cleaned, 0)
        self.assertEqual(os.path.getsize(archive), 4000)

    def test_clean_runaway_temp_logs_skips_stale_open_log(self):
        if os.name == "nt":
            self.skipTest("POSIX 语义")
        temp_watch = os.path.join(self.temp_dir, "mock_tmp_stale")
        os.makedirs(temp_watch, exist_ok=True)
        log = os.path.join(temp_watch, "service.log")
        with open(log, "w") as f:
            f.write("X" * 4000)
        old = time.time() - 3600
        os.utime(log, (old, old))
        holder = open(log, "r")
        try:
            cleaned, _ = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
        finally:
            holder.close()
        self.assertEqual(cleaned, 0, "一小时未增长的日志不是正在失控的日志")

    def test_reaper_program_name_matching(self):
        """回归：子串匹配让 'go' 命中 Google Chrome、'python' 命中 Toolkit 自己的 router，reaper 永远不触发。"""
        killed = []
        orphan_pid = 424242
        procs = [
            (orphan_pid, "/Applications/WorkBuddy.app/Contents/Resources/sandbox-center --serve"),
            (1001, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --type=renderer"),
            (1002, "/usr/bin/python3 /Users/runner/.workbuddy/toolkit/sidecar/router_engine.py"),
            (1003, "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin) --type=utility"),
            (1004, "/usr/sbin/mDNSResponder"),
        ]
        orig_gui, orig_kill = self.guard.is_workbuddy_gui_running, self.guard.os.kill
        self.guard.is_workbuddy_gui_running = lambda: False
        self.guard.os.kill = lambda pid, sig: killed.append(pid)
        try:
            res = self.guard.reap_orphaned_daemons(proc_list=procs, lock_dir=self.temp_dir)
            self.assertTrue(res.get("reaped"), res)
            self.assertEqual(killed, [orphan_pid])

            killed.clear()
            res = self.guard.reap_orphaned_daemons(lock_dir=self.temp_dir, proc_list=procs + [(2001, "python3 train.py --epochs 10")])
            self.assertEqual(res.get("reason"), "user_workload_active")
            res = self.guard.reap_orphaned_daemons(lock_dir=self.temp_dir, proc_list=procs + [(2002, "/opt/homebrew/bin/go build ./...")])
            self.assertEqual(res.get("reason"), "user_workload_active")
            self.assertEqual(killed, [])
        finally:
            self.guard.is_workbuddy_gui_running = orig_gui
            self.guard.os.kill = orig_kill


if __name__ == '__main__':
    unittest.main()


