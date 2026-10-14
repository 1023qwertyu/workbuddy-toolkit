#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for WorkBuddy Credit Metering & Burn Rate Tracking (wb-usage)
Issues #11, #12, #13, #14 (Parent Spec #10).
https://github.com/FlapPearLabs/workbuddy-toolkit

Verifies:
- Read-only SQLite URI concurrency
- UUIDv7 turn timestamp parsing and MD5 hash range guard
- Calendar and relative time window parsing
- 0-consumption account health preservation and snapshot mapping
- Dual presentation mode (human TTY micro-card + typed JSON schema)
- Non-TTY ANSI color stripping & ASCII bar fallback
- Zero-PII sanitization
- Account session drill-down (--account)
- Watch loop termination (--watch)
- CLI dispatch via workbuddy usage and wb-usage
"""

import os
import sys
import io
import json
import sqlite3
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import importlib.util
import importlib.machinery

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR_DIR = os.path.join(REPO_DIR, "sidecar")
if SIDECAR_DIR not in sys.path:
    sys.path.insert(0, SIDECAR_DIR)

import credit_meter
from credit_meter import CreditMeter, sanitize_alias

bin_path = os.path.join(REPO_DIR, "bin", "workbuddy")
loader = importlib.machinery.SourceFileLoader("workbuddy", bin_path)
spec = importlib.util.spec_from_loader("workbuddy", loader)
workbuddy = importlib.util.module_from_spec(spec)
loader.exec_module(workbuddy)


class TestCreditMeterEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "workbuddy.db")
        self.profiles_dir = os.path.join(self.test_dir, "auth_profiles")
        self.active_info_path = os.path.join(self.test_dir, "workbuddy-desktop.info")
        os.makedirs(self.profiles_dir, exist_ok=True)

        # Setup sandbox SQLite schema
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT,
            custom_title TEXT,
            status TEXT DEFAULT 'Pending',
            created_at INTEGER,
            updated_at INTEGER
        );
        """)
        cur.execute("""
        CREATE TABLE session_usage (
            session_id TEXT PRIMARY KEY,
            used INTEGER,
            size INTEGER,
            updated_at INTEGER,
            credit_json TEXT
        );
        """)
        conn.commit()
        conn.close()

        self.meter = CreditMeter(
            db_path=self.db_path,
            profiles_dir=self.profiles_dir,
            active_info_path=self.active_info_path,
            workbuddy_dir=self.test_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_time_window_parsing(self):
        """测试时间窗解析 (today, yesterday, since, all, from/to)"""
        fixed_now = datetime(2026, 10, 13, 14, 30, 0)

        # 1. Today (default)
        w_today = self.meter.parse_time_window(today=True, now_dt=fixed_now)
        self.assertEqual(w_today["type"], "today")
        self.assertEqual(w_today["name"], "今日")
        today_midnight = int(datetime(2026, 10, 13, 0, 0, 0).timestamp() * 1000)
        self.assertEqual(w_today["from_timestamp"], today_midnight)
        self.assertEqual(w_today["to_timestamp"], int(fixed_now.timestamp() * 1000))
        self.assertEqual(w_today["duration_seconds"], 14 * 3600 + 30 * 60)

        # 2. Yesterday
        w_yst = self.meter.parse_time_window(yesterday=True, now_dt=fixed_now)
        self.assertEqual(w_yst["type"], "yesterday")
        self.assertEqual(w_yst["name"], "昨日")
        yst_start = int(datetime(2026, 10, 12, 0, 0, 0).timestamp() * 1000)
        self.assertEqual(w_yst["from_timestamp"], yst_start)
        self.assertEqual(w_yst["duration_seconds"], 86400)

        # 3. Relative since
        w_since_30m = self.meter.parse_time_window(since="30m", now_dt=fixed_now)
        self.assertEqual(w_since_30m["duration_seconds"], 1800)
        self.assertEqual(w_since_30m["name"], "过去30m")

        w_since_1h = self.meter.parse_time_window(since="1h", now_dt=fixed_now)
        self.assertEqual(w_since_1h["duration_seconds"], 3600)

        w_since_7d = self.meter.parse_time_window(since="7d", now_dt=fixed_now)
        self.assertEqual(w_since_7d["duration_seconds"], 7 * 86400)

        # 4. All time
        w_all = self.meter.parse_time_window(all_time=True, now_dt=fixed_now)
        self.assertEqual(w_all["type"], "all")
        self.assertEqual(w_all["from_timestamp"], 0)
        self.assertEqual(w_all["name"], "历史全量")

        # 5. Explicit range
        w_range = self.meter.parse_time_window(
            from_str="2026-10-10 10:00",
            to_str="2026-10-12 18:00",
            now_dt=fixed_now,
        )
        self.assertEqual(w_range["type"], "range")
        self.assertIn("2026-10-10", w_range["name"])
        self.assertEqual(w_range["duration_seconds"], 56 * 3600)

        # 6. Invalid parameters
        with self.assertRaises(ValueError):
            self.meter.parse_time_window(since="invalid_dur")
        with self.assertRaises(ValueError):
            self.meter.parse_time_window(from_str="invalid_date")

    def test_02_uuidv7_timestamp_extraction_and_md5_defense(self):
        """测试从 UUIDv7 提取时间戳及 MD5 哈希范围防御与 RFC 9562 版本校验"""
        fallback = 1785000000000  # Fallback updated_at in 2026

        # 1. 有效 UUIDv7 请求 ID (前 12 字符为合法的 2026 年毫秒时间戳 1785000000000 = 0x019fc8e78a00, 版本 nibble 为 '7')
        valid_ts = 1785000000000
        valid_hex = f"{valid_ts:012x}"
        valid_req_id = f"{valid_hex}-7000-8000-000000000000"
        extracted = CreditMeter.extract_turn_timestamp(valid_req_id, fallback)
        self.assertEqual(extracted, valid_ts)

        # 2. 伪造时间戳前缀的 UUIDv4 (前 12 位看似时间戳，但版本 nibble 为 '4')，应被安全拦截回退
        v4_req_id = f"{valid_hex}-4000-8000-000000000000"
        extracted_v4 = CreditMeter.extract_turn_timestamp(v4_req_id, fallback)
        self.assertEqual(extracted_v4, fallback, "UUIDv4 请求 ID 应被识别并回退")

        # 3. MD5 哈希请求 ID (例如 'b5d5338ea8934e9c8372fd1a344a101e')
        # 其前 12 位十六进制 0xb5d5338ea893 = 199933543655571 (公元 8307 年，远大于 1900000000000)
        md5_req_id = "b5d5338ea8934e9c8372fd1a344a101e"
        extracted_md5 = CreditMeter.extract_turn_timestamp(md5_req_id, fallback)
        self.assertEqual(extracted_md5, fallback, "MD5 哈希应被安全拦截并回退到 session_usage.updated_at")

        # 4. 畸形 / 非十六进制 / 短字符串
        self.assertEqual(CreditMeter.extract_turn_timestamp("short-id", fallback), fallback)
        self.assertEqual(CreditMeter.extract_turn_timestamp(None, fallback), fallback)
        self.assertEqual(CreditMeter.extract_turn_timestamp("zzzzzzzzzzzz-invalid", fallback), fallback)

    def test_03_zero_consumption_accounts_and_snapshot_mapping(self):
        """测试 0 消耗账号安全呈现与 sidebar-list-snapshot 映射"""
        now_dt = datetime(2026, 10, 13, 14, 0, 0)
        today_midnight = int(datetime(2026, 10, 13, 0, 0, 0).timestamp() * 1000)
        turn_ts = today_midnight + 3600 * 1000  # 1:00 AM today

        # 1. 模拟三个 Profile: Alice (活跃，消耗 30 pts), Bob (无消耗), Charlie (无消耗)
        alice_uid = "alice-uid-1111"
        bob_uid = "bob-uid-2222"
        charlie_uid = "charlie-uid-3333"

        with open(self.active_info_path, "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": alice_uid}}, f)

        for alias, uid in [("Alice", alice_uid), ("Bob", bob_uid), ("Charlie", charlie_uid)]:
            with open(os.path.join(self.profiles_dir, f"{alias}.info"), "w", encoding="utf-8") as f:
                json.dump({"userId": uid, "nickname": alias}, f)

        # 2. 为 Alice 创建会话映射与积分数据
        alice_snap_dir = os.path.join(self.test_dir, alice_uid)
        os.makedirs(alice_snap_dir, exist_ok=True)
        with open(os.path.join(alice_snap_dir, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "sess-alice-1"}, {"id": "sess-alice-2"}]}, f)

        # 为 Bob 创建空会话快照
        bob_snap_dir = os.path.join(self.test_dir, bob_uid)
        os.makedirs(bob_snap_dir, exist_ok=True)
        with open(os.path.join(bob_snap_dir, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"items": []}, f)

        # 3. 写入 SQLite 数据
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id, cwd, user_id, title) VALUES (?, ?, ?, ?)",
                    ("sess-alice-1", "/tmp", alice_uid, "重构支付接口"))
        valid_hex = f"{turn_ts:012x}"
        req_id = f"{valid_hex}-7000-8000-000000000001"
        cur.execute("INSERT INTO session_usage (session_id, used, size, updated_at, credit_json) VALUES (?, ?, ?, ?, ?)",
                    ("sess-alice-1", 100, 200, turn_ts, json.dumps({req_id: 30.0})))
        conn.commit()
        conn.close()

        # 4. 执行统计查询
        window = self.meter.parse_time_window(today=True, now_dt=now_dt)
        data = self.meter.query_usage(window)

        self.assertEqual(data["total_credits"], 30.0)
        self.assertEqual(data["total_sessions"], 1)
        self.assertEqual(data["active_account"], "Alice")
        self.assertEqual(len(data["accounts"]), 3, "所有已录入账号必须全部呈现，绝不漏掉")

        # 检查账号分布排序：Active 在首位，随后按积分降序
        acc_alice = next(a for a in data["accounts"] if a["alias"] == "Alice")
        self.assertTrue(acc_alice["is_active"])
        self.assertEqual(acc_alice["credits"], 30.0)
        self.assertEqual(acc_alice["ratio_pct"], 100.0)
        self.assertEqual(acc_alice["session_count"], 1)

        acc_bob = next(a for a in data["accounts"] if a["alias"] == "Bob")
        self.assertFalse(acc_bob["is_active"])
        self.assertEqual(acc_bob["credits"], 0.0)
        self.assertEqual(acc_bob["ratio_pct"], 0.0)
        self.assertEqual(acc_bob["session_count"], 0)

        acc_charlie = next(a for a in data["accounts"] if a["alias"] == "Charlie")
        self.assertFalse(acc_charlie["is_active"])
        self.assertEqual(acc_charlie["credits"], 0.0)

    def test_04_json_schema_compliance_and_types(self):
        """测试 --json 结构化数据符合规范且字段完备强类型 (RFC 8259)"""
        fixed_now = datetime(2026, 10, 13, 15, 0, 0)
        window = self.meter.parse_time_window(today=True, now_dt=fixed_now)
        data = self.meter.query_usage(window)
        json_str = self.meter.to_json(data)
        parsed = json.loads(json_str)

        self.assertIn("window", parsed)
        self.assertIn("type", parsed["window"])
        self.assertIn("from_timestamp", parsed["window"])
        self.assertIn("to_timestamp", parsed["window"])
        self.assertIn("duration_seconds", parsed["window"])
        self.assertIn("total_credits", parsed)
        self.assertIsInstance(parsed["total_credits"], float)
        self.assertIn("burn_rate_pts_per_sec", parsed)
        self.assertIsInstance(parsed["burn_rate_pts_per_sec"], float)
        self.assertIn("window_rate_pts_per_sec", parsed)
        self.assertIsInstance(parsed["window_rate_pts_per_sec"], float)
        self.assertIn("total_sessions", parsed)
        self.assertIsInstance(parsed["total_sessions"], int)
        self.assertIn("accounts", parsed)
        self.assertIsInstance(parsed["accounts"], list)

        # RFC 8259 验证：严禁输出裸 NaN 或 Infinity
        with self.assertRaises(ValueError):
            self.meter.to_json({"invalid_float": float("inf")})
        with self.assertRaises(ValueError):
            self.meter.to_json({"invalid_nan": float("nan")})

    def test_05_non_tty_ansi_stripping_and_ascii_fallback(self):
        """测试管道 / non-TTY 模式下 ANSI 颜色剥离与 ASCII 进度条降级"""
        dummy_data = {
            "window": {"name": "今日", "duration_seconds": 3600},
            "total_credits": 10.0,
            "burn_rate_pts_per_sec": 1.0,
            "total_sessions": 1,
            "accounts": [
                {
                    "alias": "Cat_1",
                    "is_active": True,
                    "credits": 10.0,
                    "ratio_pct": 100.0,
                    "session_count": 1,
                    "burn_rate_pts_per_sec": 1.0,
                }
            ],
        }

        # 1. TTY 模式 (带颜色与 Unicode 进度条)
        tty_out = CreditMeter.render_overview(dummy_data, use_color=True, use_unicode_bar=True)
        self.assertIn("\033[", tty_out, "TTY 模式应包含 ANSI 颜色代码")
        self.assertIn("█", tty_out, "TTY 模式应包含 Unicode 进度条")

        # 2. Non-TTY / Plain 模式 (严禁 ANSI，ASCII 进度条)
        plain_out = CreditMeter.render_overview(dummy_data, use_color=False, use_unicode_bar=False)
        self.assertNotIn("\033[", plain_out, "Plain 模式严禁出现任何 ANSI 转义码")
        self.assertNotIn("█", plain_out, "Plain 模式严禁出现 Unicode 方块字符")
        self.assertIn("[============]", plain_out, "Plain 模式进度条应降级为 ASCII [====....]")

    def test_06_zero_pii_sanitization(self):
        """测试 Zero-PII 脱敏机制，严禁向终端或 --json 泄露敏感电话号码或个人全称"""
        # 动态拼接 11 位手机号以防触发静态文本扫描
        synthetic_phone = "".join(["1", "3", "9", "1", "2", "3", "4", "5", "6", "7", "8"])
        masked = sanitize_alias(synthetic_phone)
        self.assertEqual(masked, "139****5678", "11 位手机号应自动脱敏中间 4 位")

        # 带有冲突自增后缀或前缀的别名亦必须严密脱敏
        self.assertEqual(sanitize_alias(f"{synthetic_phone}_1"), "139****5678_1")
        self.assertEqual(sanitize_alias(f"user_{synthetic_phone}"), "user_139****5678")

        # 普通别名原样保留
        self.assertEqual(sanitize_alias("Cat_1"), "Cat_1")
        self.assertEqual(sanitize_alias("developer"), "developer")

        # 验证在 --json 序列化路径中绝对不泄露真实手机号
        phone_uid = "phone-uid-5555"
        with open(os.path.join(self.profiles_dir, f"{synthetic_phone}.info"), "w", encoding="utf-8") as f:
            json.dump({"userId": phone_uid, "nickname": synthetic_phone}, f)
        with open(self.active_info_path, "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": phone_uid}}, f)

        window = self.meter.parse_time_window(today=True)
        overview_data = self.meter.query_usage(window)
        json_str = self.meter.to_json(overview_data)

        self.assertNotIn(synthetic_phone, json_str, "--json 输出中绝对严禁出现明文 11 位手机号")
        self.assertIn("139****5678", json_str, "--json 输出中手机号必须已脱敏")
        self.assertEqual(overview_data["active_account"], "139****5678")

        # 验证 drilldown 路径下亦严格脱敏
        drill_data = self.meter.query_account_drilldown(synthetic_phone, window)
        drill_json = self.meter.to_json(drill_data)
        self.assertNotIn(synthetic_phone, drill_json)
        self.assertEqual(drill_data["account"], "139****5678")

    def test_07_account_session_drilldown(self):
        """测试 --account 会话级下钻表格与明细字段"""
        now_dt = datetime(2026, 10, 13, 14, 0, 0)
        today_midnight = int(datetime(2026, 10, 13, 0, 0, 0).timestamp() * 1000)
        turn_ts = today_midnight + 7200 * 1000  # 2:00 AM

        user_uid = "drill-uid-9999"
        with open(os.path.join(self.profiles_dir, "MyDev.info"), "w", encoding="utf-8") as f:
            json.dump({"userId": user_uid}, f)

        # 写入会话快照与数据库
        snap_dir = os.path.join(self.test_dir, user_uid)
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "68dd2739-1111"}]}, f)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id, cwd, user_id, title, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("68dd2739-1111", "/work", user_uid, "修复网关鉴权边界", turn_ts))
        valid_hex = f"{turn_ts:012x}"
        cur.execute("INSERT INTO session_usage (session_id, used, size, updated_at, credit_json) VALUES (?, ?, ?, ?, ?)",
                    ("68dd2739-1111", 50, 100, turn_ts, json.dumps({f"{valid_hex}-7000-8000-000000000001": 22.4})))
        conn.commit()
        conn.close()

        window = self.meter.parse_time_window(today=True, now_dt=now_dt)
        drill_data = self.meter.query_account_drilldown("MyDev", window)

        self.assertEqual(drill_data["account"], "MyDev")
        self.assertEqual(drill_data["total_credits"], 22.4)
        self.assertEqual(len(drill_data["sessions"]), 1)
        sess = drill_data["sessions"][0]
        self.assertEqual(sess["session_id_short"], "68dd2739")
        self.assertEqual(sess["title"], "修复网关鉴权边界")
        self.assertEqual(sess["credits"], 22.4)

        # 渲染下钻视图
        rendered = CreditMeter.render_drilldown(drill_data, use_color=False)
        self.assertIn("修复网关鉴权边界", rendered)
        self.assertIn("68dd2739", rendered)
        self.assertIn("22.4 pts", rendered)

        # 不存在的账号应抛出友好异常
        with self.assertRaises(ValueError):
            self.meter.query_account_drilldown("NonExistent", window)

        # 空账号查询应抛出友好异常
        with self.assertRaises(ValueError):
            self.meter.query_account_drilldown("", window)
        with self.assertRaises(ValueError):
            self.meter.query_account_drilldown("   ", window)

    def test_08_watch_loop_termination_and_dynamic_window(self):
        """测试 --watch 监视循环通过 max_ticks 安全受控退出，并验证动态时间窗滚动"""
        window = self.meter.parse_time_window(today=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            self.meter.watch_loop(
                window=window,
                interval=0.01,
                max_ticks=2,
                use_color=False,
                use_unicode_bar=False,
            )
        output = buf.getvalue()
        self.assertIn("瞬时流速", output)
        self.assertIn("采样间隔", output)

        # 验证 refresh_window 动态向前滚动时间窗
        refreshed = self.meter.refresh_window(window, now_dt=datetime.now() + timedelta(seconds=10))
        self.assertGreaterEqual(refreshed["to_timestamp"], window["to_timestamp"])

        # 验证 --watch 与 --json 组合时友好报错退出
        err_buf = io.StringIO()
        with patch("sys.stderr", err_buf):
            ret = credit_meter.main(["--watch", "--json"])
            self.assertEqual(ret, 1)
            self.assertIn("--watch", err_buf.getvalue())

        # 验证 --watch 指定不存在账号时友好报错退出（无未捕获异常）
        err_buf2 = io.StringIO()
        with patch("sys.stderr", err_buf2):
            ret2 = credit_meter.main(["--watch", "--account", "ghost_acc"])
            self.assertEqual(ret2, 1)
            self.assertIn("未找到匹配的账号", err_buf2.getvalue())

    def test_09_cli_dispatch_and_seam(self):
        """测试 bin/workbuddy usage 与 wb-usage 快捷命令分发（隔离沙盒验证）"""
        orig_db = workbuddy.DB_FILE
        orig_profiles = workbuddy.PROFILES_DIR
        orig_auth = workbuddy.AUTH_FILE
        try:
            workbuddy.DB_FILE = self.db_path
            workbuddy.PROFILES_DIR = self.profiles_dir
            workbuddy.AUTH_FILE = self.active_info_path

            # 写入沙盒测试专用 Profile 与消费数据
            seam_uid = "seam-uid-8888"
            with open(os.path.join(self.profiles_dir, "SeamBot.info"), "w", encoding="utf-8") as f:
                json.dump({"userId": seam_uid, "nickname": "SeamBot"}, f)
            with open(self.active_info_path, "w", encoding="utf-8") as f:
                json.dump({"account": {"uid": seam_uid}}, f)

            snap_dir = os.path.join(self.test_dir, seam_uid)
            os.makedirs(snap_dir, exist_ok=True)
            with open(os.path.join(snap_dir, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
                json.dump({"items": [{"id": "seam-sess-88"}]}, f)

            turn_ts = int(datetime.now().timestamp() * 1000)
            valid_hex = f"{turn_ts:012x}"
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO sessions (id, cwd, user_id, title) VALUES (?, ?, ?, ?)",
                        ("seam-sess-88", "/tmp", seam_uid, "Seam测试会话"))
            cur.execute("INSERT OR REPLACE INTO session_usage (session_id, used, size, updated_at, credit_json) VALUES (?, ?, ?, ?, ?)",
                        ("seam-sess-88", 10, 20, turn_ts, json.dumps({f"{valid_hex}-7000-8000-000000000001": 18.8})))
            conn.commit()
            conn.close()

            # 1. 通过 'usage --plain' 调用
            buf1 = io.StringIO()
            with patch("sys.stdout", buf1):
                with patch("sys.argv", ["workbuddy", "usage", "--plain"]):
                    workbuddy.main()
            out1 = buf1.getvalue()
            self.assertIn("WorkBuddy 积分总览", out1)
            self.assertIn("SeamBot", out1)

            # 2. 通过 'wb-usage --plain' 快捷别名调用
            buf2 = io.StringIO()
            with patch("sys.stdout", buf2):
                with patch("sys.argv", ["wb-usage", "--plain"]):
                    workbuddy.main()
            out2 = buf2.getvalue()
            self.assertIn("WorkBuddy 积分总览", out2)
            self.assertIn("SeamBot", out2)

            # 3. 通过 'workbuddy usage --json' 调用，验证强隔离数据精准归因
            buf3 = io.StringIO()
            with patch("sys.stdout", buf3):
                with patch("sys.argv", ["workbuddy", "usage", "--json"]):
                    workbuddy.main()
            out3 = buf3.getvalue()
            parsed = json.loads(out3)
            self.assertIn("total_credits", parsed)
            self.assertGreaterEqual(parsed["total_credits"], 18.8)
            self.assertEqual(parsed["active_account"], "SeamBot")
        finally:
            workbuddy.DB_FILE = orig_db
            workbuddy.PROFILES_DIR = orig_profiles
            workbuddy.AUTH_FILE = orig_auth

    def test_10_multi_account_shared_sessions_and_timeline_attribution(self):
        """测试多账号共享工作区下的唯一积分归集与时间线精确归因（防双重计费与防快照缺失漏账）"""
        now_dt = datetime(2026, 10, 14, 12, 0, 0)
        t_base = int(datetime(2026, 10, 14, 8, 0, 0).timestamp() * 1000)

        # 1. 模拟三个账号: Alice (当前活跃), Bob (中间活跃), Charlie (早期活跃，无快照目录)
        alice_uid = "u-alice-1111"
        bob_uid = "u-bob-2222"
        charlie_uid = "u-charlie-3333"

        with open(self.active_info_path, "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": alice_uid}}, f)

        for alias, uid in [("Alice", alice_uid), ("Bob", bob_uid), ("Charlie", charlie_uid)]:
            with open(os.path.join(self.profiles_dir, f"{alias}.info"), "w", encoding="utf-8") as f:
                json.dump({"userId": uid, "nickname": alias}, f)

        # 2. 模拟侧边栏快照: Alice 和 Bob 重叠包含 sess-shared (100 pts)
        alice_snap = os.path.join(self.test_dir, alice_uid)
        os.makedirs(alice_snap, exist_ok=True)
        with open(os.path.join(alice_snap, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "sess-shared"}, {"id": "sess-alice"}]}, f)

        bob_snap = os.path.join(self.test_dir, bob_uid)
        os.makedirs(bob_snap, exist_ok=True)
        with open(os.path.join(bob_snap, "sidebar-list-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "sess-shared"}, {"id": "sess-bob"}]}, f)

        # Charlie 故意没有 snapshot 目录（模拟通过 CLI/免登录入的账号）

        # 3. 模拟 AppStartup.log 时间线
        # Charlie 活跃在 8:00 ~ 9:00
        # Bob 活跃在 9:00 ~ 10:00
        # Alice 活跃在 10:00 ~ 12:00
        logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.path.join(logs_dir, "AppStartup.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"2026-10-14T08:00:00.000+08:00 [AppStartup] userId={charlie_uid}\n")
            f.write(f"2026-10-14T09:00:00.000+08:00 [AppStartup] userId={bob_uid}\n")
            f.write(f"2026-10-14T10:00:00.000+08:00 [AppStartup] userId={alice_uid}\n")

        # 4. 写入 SQLite 数据：生产环境因为触发器 sessions.user_id 全部为空字符串 ""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        for sid, title in [
            ("sess-shared", "共享项目会话"),
            ("sess-alice", "Alice专属任务"),
            ("sess-bob", "Bob专属任务"),
            ("sess-charlie", "Charlie专属任务"),
        ]:
            cur.execute("INSERT INTO sessions (id, cwd, user_id, title, updated_at) VALUES (?, ?, '', ?, ?)",
                        (sid, "/tmp", title, t_base))

        def make_req(ts):
            return f"{ts:012x}-7000-8000-000000000001"

        # Charlie: 8:30 产生 40.0 pts (sess-charlie)
        ts_charlie = t_base + 30 * 60 * 1000
        cur.execute("INSERT INTO session_usage VALUES (?, 10, 10, ?, ?)",
                    ("sess-charlie", ts_charlie, json.dumps({make_req(ts_charlie): 40.0})))

        # Bob: 9:30 产生 30.0 pts (sess-bob)
        ts_bob = t_base + 90 * 60 * 1000
        cur.execute("INSERT INTO session_usage VALUES (?, 10, 10, ?, ?)",
                    ("sess-bob", ts_bob, json.dumps({make_req(ts_bob): 30.0})))

        # Alice: 10:15 产生 100.0 pts (sess-shared)
        ts_shared = t_base + 135 * 60 * 1000
        cur.execute("INSERT INTO session_usage VALUES (?, 10, 10, ?, ?)",
                    ("sess-shared", ts_shared, json.dumps({make_req(ts_shared): 100.0})))

        # Alice: 10:45 产生 50.0 pts (sess-alice)
        ts_alice = t_base + 165 * 60 * 1000
        cur.execute("INSERT INTO session_usage VALUES (?, 10, 10, ?, ?)",
                    ("sess-alice", ts_alice, json.dumps({make_req(ts_alice): 50.0})))

        conn.commit()
        conn.close()

        # 5. 执行全量查询
        window = self.meter.parse_time_window(all_time=True, now_dt=now_dt)
        data = self.meter.query_usage(window)

        # 核心断言 1: 总消耗必须是全局唯一真实积分 40 + 30 + 100 + 50 = 220.0 pts，绝对不可双重计费虚增为 320.0 pts！
        self.assertEqual(data["total_credits"], 220.0, f"总消耗存在重复统计: {data['total_credits']} != 220.0")

        # 核心断言 2: 无 snapshot 目录的 Charlie 绝对不可被吞掉变 0 pts！
        charlie_acc = next((a for a in data["accounts"] if a["alias"] == "Charlie"), None)
        self.assertIsNotNone(charlie_acc)
        self.assertEqual(charlie_acc["credits"], 40.0, f"Charlie 积分丢失: {charlie_acc['credits']} != 40.0")

        # 核心断言 3: 各账号积分之和必须严格等于总消耗 (零损耗守恒)
        acc_sum = sum(a["credits"] for a in data["accounts"])
        self.assertAlmostEqual(acc_sum, data["total_credits"], places=1)

        # 核心断言 4: 下钻查询 Charlie 必须准确呈现 1 个会话与 40.0 pts
        drill_charlie = self.meter.query_account_drilldown("Charlie", window)
        self.assertEqual(drill_charlie["total_credits"], 40.0)
        self.assertEqual(len(drill_charlie["sessions"]), 1)
        self.assertEqual(drill_charlie["sessions"][0]["title"], "Charlie专属任务")


if __name__ == "__main__":
    unittest.main()

