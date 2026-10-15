#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零散加固项的回归测试：原子写入、UUIDv7 时间上界、估算流速标注、颜色输出、原型脚本可运行。"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import importlib.util
import importlib.machinery

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_PATH = os.path.join(REPO_DIR, "bin", "workbuddy")
_loader = importlib.machinery.SourceFileLoader("workbuddy_misc", BIN_PATH)
_spec = importlib.util.spec_from_loader("workbuddy_misc", _loader)
workbuddy = importlib.util.module_from_spec(_spec)
_loader.exec_module(workbuddy)

sys.path.insert(0, os.path.join(REPO_DIR, "sidecar"))
from credit_meter import CreditMeter  # noqa: E402
import credit_meter  # noqa: E402


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crash_mid_write_keeps_previous_content(self):
        path = os.path.join(self.tmp, "acc.info")
        workbuddy.atomic_write_json(path, {"account": {"uid": "u1"}})
        real_dump = json.dump

        def exploding_dump(obj, fp, *a, **kw):
            fp.write('{"account": {"ui')  # 半截 JSON
            raise OSError("disk full")

        with patch.object(workbuddy.json, "dump", side_effect=exploding_dump):
            with self.assertRaises(OSError):
                workbuddy.atomic_write_json(path, {"account": {"uid": "u2"}})
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"account": {"uid": "u1"}})
        self.assertEqual(os.listdir(self.tmp), ["acc.info"], "临时文件必须被清理")
        self.assertIs(json.dump, real_dump)

    @unittest.skipIf(sys.platform == "win32", "POSIX 权限位")
    def test_written_files_are_private(self):
        path = os.path.join(self.tmp, "sub", "acc.info")
        workbuddy.atomic_write_json(path, {"k": "v"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class TestUuidV7Bounds(unittest.TestCase):
    def _v7(self, ms, variant="8"):
        h = f"{ms:012x}"
        return f"{h[:8]}-{h[8:12]}-7abc-{variant}def-0123456789ab"

    def test_timestamps_after_2030_are_still_extracted(self):
        ts_2031 = int(datetime(2031, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(CreditMeter.extract_turn_timestamp(self._v7(ts_2031), 0), ts_2031)

    def test_invalid_variant_bits_fall_back(self):
        ts = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(CreditMeter.extract_turn_timestamp(self._v7(ts, variant="8"), 0), ts)
        self.assertEqual(CreditMeter.extract_turn_timestamp(self._v7(ts, variant="0"), 42), 42)


class TestBurnRateIsLabelledEstimate(unittest.TestCase):
    def test_usage_json_and_card_mark_estimate(self):
        tmp = tempfile.mkdtemp()
        try:
            meter = CreditMeter(db_path=os.path.join(tmp, "missing.db"), profiles_dir=os.path.join(tmp, "p"),
                                active_info_path=os.path.join(tmp, "none.info"), workbuddy_dir=tmp)
            data = meter.query_usage(meter.parse_time_window())
            self.assertIs(data["burn_rate_is_estimate"], True)
            self.assertEqual(data["assumed_seconds_per_turn"], credit_meter.ASSUMED_SECONDS_PER_TURN)
            self.assertIn("估算流速", CreditMeter.render_overview(data, use_color=False, use_unicode_bar=False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestColorAndScripts(unittest.TestCase):
    def test_no_ansi_when_stdout_is_not_a_tty(self):
        code = ("import importlib.machinery as m, importlib.util as u;"
                f"l=m.SourceFileLoader('w', {BIN_PATH!r});s=u.spec_from_loader('w', l);w=u.module_from_spec(s);l.exec_module(w);"
                "print(repr(w.COLOR_GREEN))")
        env = dict(os.environ)
        env.pop("NO_COLOR", None)
        out = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.assertEqual(out.stdout.strip(), "''", out.stderr)

    def test_prototype_usage_ui_runs(self):
        res = subprocess.run([sys.executable, os.path.join(REPO_DIR, "scripts", "prototype_usage_ui.py"), "--mock"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
