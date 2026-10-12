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

        other_file = os.path.join(temp_watch, "large.data")
        with open(other_file, "w") as f:
            f.write("D" * 2000)

        # Max size 0.0005 MB (~500 bytes), so oversized_log (>4000 bytes) will be truncated
        cleaned, freed = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
        self.assertEqual(cleaned, 1)
        self.assertGreaterEqual(freed, file_size)
        self.assertTrue(os.path.exists(oversized_log))
        self.assertEqual(os.path.getsize(oversized_log), 0)
        self.assertEqual(os.path.getsize(normal_log), 3)
        self.assertEqual(os.path.getsize(other_file), 2000)

    def test_clean_runaway_temp_logs_windows_locked_skip(self):
        original_name = os.name
        os.name = "nt"
        temp_watch = os.path.join(self.temp_dir, "mock_tmp_win")
        os.makedirs(temp_watch, exist_ok=True)
        locked_log = os.path.join(temp_watch, "locked_runaway.log")
        with open(locked_log, "w") as f:
            f.write("E" * 2000)

        original_is_file_open = self.guard.is_file_open
        self.guard.is_file_open = lambda fp, ofs: True

        try:
            cleaned, freed = self.guard.clean_runaway_temp_logs([temp_watch], max_temp_file_mb=0.0005)
            self.assertEqual(cleaned, 0)
            self.assertEqual(freed, 0)
            self.assertEqual(os.path.getsize(locked_log), 2000)
        finally:
            self.guard.is_file_open = original_is_file_open
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

        try:
            self.guard.main()
            self.assertTrue(os.path.exists(oversized_log))
            self.assertEqual(os.path.getsize(oversized_log), 0)
        finally:
            if "WB_TMP_DIR" in os.environ:
                del os.environ["WB_TMP_DIR"]
            if "WB_MAX_TEMP_FILE_MB" in os.environ:
                del os.environ["WB_MAX_TEMP_FILE_MB"]


if __name__ == '__main__':
    unittest.main()


