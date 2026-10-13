#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Credit Metering & Burn Rate Engine (wb-usage)
Deep Module Seam for Credit & Velocity Attribution.
https://github.com/FlapPearLabs/workbuddy-toolkit

Implements:
- Read-only concurrent SQLite access (mode=ro, uri=True, timeout=5.0s)
- Turn-level timestamp attribution (UUIDv7 extraction with MD5 range guard)
- Time window filtering (today, yesterday, since, all, from/to)
- Per-account credit attribution & 0-consumption account health preservation
- Dual-mode presentation (human TTY micro-card + machine-readable typed JSON)
- Account session drill-down (--account) & live burn rate monitor (--watch)
"""

import os
import sys
import json
import time
import glob
import re
import sqlite3
import argparse
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

# ANSI terminal colors (strictly disabled in non-TTY or plain mode)
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def sanitize_alias(alias: str) -> str:
    """Masks 11-digit mobile phone numbers for Zero-PII compliance."""
    if re.match(r"^1[3-9]\d{9}$", alias):
        return f"{alias[:3]}****{alias[7:]}"
    return alias


class CreditMeter:
    """Isolated deep module engine for WorkBuddy credit consumption and burn-rate tracking."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        profiles_dir: Optional[str] = None,
        active_info_path: Optional[str] = None,
        workbuddy_dir: Optional[str] = None,
    ):
        self.workbuddy_dir = workbuddy_dir or os.path.expanduser("~/.workbuddy")
        self.db_path = db_path or os.path.join(self.workbuddy_dir, "workbuddy.db")
        self.profiles_dir = profiles_dir or os.path.join(self.workbuddy_dir, "auth_profiles")
        self.active_info_path = active_info_path or self._resolve_default_active_info()

    @staticmethod
    def _resolve_default_active_info() -> str:
        if sys.platform == "darwin":
            candidates = [
                os.path.expanduser("~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
                os.path.expanduser("~/.workbuddy/auth/workbuddy-desktop.info"),
            ]
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
            localappdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
            userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
            candidates = [
                os.path.join(appdata, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
                os.path.join(localappdata, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
                os.path.join(userprofile, ".workbuddy", "auth", "workbuddy-desktop.info"),
            ]
        else:
            config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
            candidates = [
                os.path.join(config_home, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
                os.path.join(data_home, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
                os.path.expanduser("~/.workbuddy/auth/workbuddy-desktop.info"),
            ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return candidates[0]

    def _get_readonly_conn(self) -> Optional[sqlite3.Connection]:
        if not os.path.exists(self.db_path):
            return None
        try:
            abs_path = os.path.abspath(self.db_path)
            file_url = f"file:{urllib.request.pathname2url(abs_path)}?mode=ro"
            return sqlite3.connect(file_url, uri=True, timeout=5.0)
        except Exception:
            try:
                return sqlite3.connect(self.db_path, timeout=5.0)
            except Exception:
                return None

    def parse_time_window(
        self,
        today: bool = False,
        yesterday: bool = False,
        since: Optional[str] = None,
        all_time: bool = False,
        from_str: Optional[str] = None,
        to_str: Optional[str] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Parses command-line time parameters into milliseconds epoch boundaries."""
        now = now_dt or datetime.now()

        # Explicit date range
        if from_str:
            from_dt = self._parse_datetime(from_str)
            to_dt = self._parse_datetime(to_str) if to_str else now
            from_ts = int(from_dt.timestamp() * 1000)
            to_ts = int(to_dt.timestamp() * 1000)
            dur_sec = max(int((to_ts - from_ts) / 1000), 1)
            name_str = f"{from_str} 至 {to_str or '当前'}"
            return {
                "type": "range",
                "name": name_str,
                "from_timestamp": from_ts,
                "to_timestamp": to_ts,
                "duration_seconds": dur_sec,
            }

        # Yesterday full 24h
        if yesterday:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yst_start = today_start - timedelta(days=1)
            yst_end = today_start - timedelta(milliseconds=1)
            from_ts = int(yst_start.timestamp() * 1000)
            to_ts = int(yst_end.timestamp() * 1000)
            return {
                "type": "yesterday",
                "name": "昨日",
                "from_timestamp": from_ts,
                "to_timestamp": to_ts,
                "duration_seconds": 86400,
            }

        # Relative since duration (e.g., 30m, 1h, 2h, 7d)
        if since:
            dur_sec = self._parse_duration(since)
            from_dt = now - timedelta(seconds=dur_sec)
            from_ts = int(from_dt.timestamp() * 1000)
            to_ts = int(now.timestamp() * 1000)
            return {
                "type": "since",
                "name": f"过去{since}",
                "from_timestamp": from_ts,
                "to_timestamp": to_ts,
                "duration_seconds": dur_sec,
            }

        # All-time lifetime historical
        if all_time:
            to_ts = int(now.timestamp() * 1000)
            return {
                "type": "all",
                "name": "历史全量",
                "from_timestamp": 0,
                "to_timestamp": to_ts,
                "duration_seconds": max(int(now.timestamp()), 1),
            }

        # Default: Today from local midnight 00:00:00 to now
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        from_ts = int(today_start.timestamp() * 1000)
        to_ts = int(now.timestamp() * 1000)
        dur_sec = max(int((to_ts - from_ts) / 1000), 1)
        return {
            "type": "today",
            "name": "今日",
            "from_timestamp": from_ts,
            "to_timestamp": to_ts,
            "duration_seconds": dur_sec,
        }

    @staticmethod
    def _parse_datetime(val: str) -> datetime:
        val = val.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                pass
        raise ValueError(f"无法识别的时间格式: '{val}'。支持格式: YYYY-MM-DD 或 YYYY-MM-DD HH:MM")

    @staticmethod
    def _parse_duration(dur: str) -> int:
        dur = dur.strip().lower()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$", dur)
        if not m:
            raise ValueError(f"无法识别的时长: '{dur}'。支持格式如: 30m, 1h, 2h, 7d, 60s")
        val = float(m.group(1))
        unit = m.group(2) or "h"
        multipliers = {
            "s": 1,
            "m": 60,
            "h": 3600,
            "d": 86400,
        }
        return int(val * multipliers[unit])

    @staticmethod
    def extract_turn_timestamp(req_id: str, fallback_ts: int) -> int:
        """Extracts UUIDv7 millisecond epoch timestamp with sanity boundaries.
        
        Sanity boundary: 1700000000000 <= ts <= 1900000000000 (Nov 2023 ~ Mar 2030).
        Protects against random MD5 hashes being erroneously parsed as future timestamps.
        """
        if isinstance(req_id, str) and len(req_id) >= 12:
            try:
                candidate = int(req_id[:12], 16)
                if 1700000000000 <= candidate <= 1900000000000:
                    return candidate
            except Exception:
                pass
        return fallback_ts

    def load_active_uid(self) -> Optional[str]:
        if os.path.exists(self.active_info_path):
            try:
                with open(self.active_info_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("account", {}).get("uid") or data.get("userId") or data.get("uid")
            except Exception:
                pass
        return None

    def load_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Loads all saved account profiles from profiles_dir.
        
        Returns mapping: alias -> {"alias": str, "uid": str, "uid_short": str, "is_active": bool}
        """
        active_uid = self.load_active_uid()
        profiles = {}

        if os.path.exists(self.profiles_dir):
            for path in glob.glob(os.path.join(self.profiles_dir, "*.info")):
                alias = os.path.splitext(os.path.basename(path))[0]
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        uid = data.get("userId") or data.get("uid") or data.get("account", {}).get("uid")
                        if uid:
                            uid_str = str(uid)
                            profiles[alias] = {
                                "alias": alias,
                                "uid": uid_str,
                                "uid_short": f"{uid_str[:8]}...",
                                "is_active": (active_uid is not None and uid_str == active_uid),
                            }
                except Exception:
                    pass

        return profiles

    def load_account_sessions(self, uid: str) -> List[str]:
        """Resolves session IDs associated with a specific user UID."""
        sids = []
        snap_path = os.path.join(self.workbuddy_dir, uid, "sidebar-list-snapshot.json")
        if os.path.exists(snap_path):
            try:
                with open(snap_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("items", []):
                        if isinstance(item, dict) and "id" in item:
                            sids.append(str(item["id"]))
            except Exception:
                pass

        # Fallback to SQLite sessions table user_id mapping
        conn = self._get_readonly_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT id FROM sessions WHERE user_id = ?", (uid,))
                for row in cur.fetchall():
                    if row[0] and row[0] not in sids:
                        sids.append(str(row[0]))
            except Exception:
                pass
            finally:
                conn.close()

        return sids

    def query_usage(self, window: Dict[str, Any]) -> Dict[str, Any]:
        """Core metering computation across all accounts within the time window."""
        profiles = self.load_profiles()
        conn = self._get_readonly_conn()

        session_credits: Dict[str, float] = {}
        session_turns: Dict[str, int] = {}
        session_last_ts: Dict[str, int] = {}

        from_ts = window["from_timestamp"]
        to_ts = window["to_timestamp"]

        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT session_id, updated_at, credit_json FROM session_usage")
                for sid, updated_at, credit_json in cur.fetchall():
                    if not sid or not credit_json:
                        continue
                    try:
                        cj = json.loads(credit_json)
                        if not isinstance(cj, dict):
                            continue
                        for req_id, pts in cj.items():
                            if not isinstance(pts, (int, float)):
                                continue
                            ts = self.extract_turn_timestamp(req_id, updated_at or 0)
                            if from_ts <= ts <= to_ts:
                                session_credits[sid] = session_credits.get(sid, 0.0) + float(pts)
                                session_turns[sid] = session_turns.get(sid, 0) + 1
                                session_last_ts[sid] = max(session_last_ts.get(sid, 0), ts)
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                conn.close()

        # Attribute sessions to accounts
        account_rows = []
        total_credits = 0.0
        total_active_turns = 0
        active_account_name = None

        for alias, prof in profiles.items():
            uid = prof["uid"]
            is_active = prof["is_active"]
            if is_active:
                active_account_name = alias

            sids = self.load_account_sessions(uid)
            acc_creds = sum(session_credits.get(s, 0.0) for s in sids)
            active_sids = [s for s in sids if session_credits.get(s, 0.0) > 0]
            acc_turns = sum(session_turns.get(s, 0) for s in sids)

            # Active generation burn rate (pts/s) during active turns
            active_seconds = max(acc_turns * 10.0, 1.0)
            burn_rate = round(acc_creds / active_seconds, 1) if acc_creds > 0 else 0.0

            acc_creds_rounded = float(round(acc_creds, 1))
            total_credits += acc_creds_rounded
            total_active_turns += acc_turns

            account_rows.append({
                "alias": alias,
                "is_active": is_active,
                "uid_short": prof["uid_short"],
                "credits": acc_creds_rounded,
                "ratio_pct": 0.0,
                "session_count": len(active_sids),
                "burn_rate_pts_per_sec": burn_rate,
            })

        total_credits = float(round(total_credits, 1))

        # Calculate ratios against total rounded credits
        for acc in account_rows:
            if total_credits > 0:
                acc["ratio_pct"] = float(round((acc["credits"] / total_credits) * 100.0, 1))

        # Sort accounts: active profile first, then by credits desc, then alias
        account_rows.sort(key=lambda x: (not x["is_active"], -x["credits"], x["alias"]))

        # Window-level burn rates
        total_credits = round(total_credits, 1)
        total_active_sec = max(total_active_turns * 10.0, 1.0)
        overall_burn_rate = round(total_credits / total_active_sec, 1) if total_credits > 0 else 0.0
        window_sec = max(window["duration_seconds"], 1)
        window_rate = round(total_credits / window_sec, 4) if total_credits > 0 else 0.0

        # Unique active sessions across all accounts
        total_sessions = sum(a["session_count"] for a in account_rows)

        return {
            "window": window,
            "total_credits": total_credits,
            "burn_rate_pts_per_sec": overall_burn_rate,
            "window_rate_pts_per_sec": window_rate,
            "total_sessions": total_sessions,
            "active_account": active_account_name,
            "accounts": account_rows,
        }

    def query_account_drilldown(self, target: str, window: Dict[str, Any]) -> Dict[str, Any]:
        """Drills down into conversation sessions for a specific account."""
        profiles = self.load_profiles()
        matched_alias = None
        matched_prof = None

        target_clean = target.strip().lower()
        for alias, prof in profiles.items():
            if alias.lower() == target_clean or prof["uid"].lower() == target_clean or prof["uid"].lower().startswith(target_clean):
                matched_alias = alias
                matched_prof = prof
                break

        if not matched_prof:
            # Check if any profile alias when sanitized matches
            for alias, prof in profiles.items():
                if sanitize_alias(alias).lower() == target_clean:
                    matched_alias = alias
                    matched_prof = prof
                    break

        if not matched_prof:
            raise ValueError(f"未找到匹配的账号 Profile: '{target}'。可通过 'wb-list' 查看已保存账号。")

        uid = matched_prof["uid"]
        sids = self.load_account_sessions(uid)

        from_ts = window["from_timestamp"]
        to_ts = window["to_timestamp"]

        session_credits: Dict[str, float] = {}
        session_turns: Dict[str, int] = {}
        session_last_ts: Dict[str, int] = {}

        conn = self._get_readonly_conn()
        titles_map: Dict[str, str] = {}
        db_updated_map: Dict[str, int] = {}

        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT session_id, updated_at, credit_json FROM session_usage")
                for sid, updated_at, credit_json in cur.fetchall():
                    if sid not in sids or not credit_json:
                        continue
                    try:
                        cj = json.loads(credit_json)
                        if not isinstance(cj, dict):
                            continue
                        for req_id, pts in cj.items():
                            if not isinstance(pts, (int, float)):
                                continue
                            ts = self.extract_turn_timestamp(req_id, updated_at or 0)
                            if from_ts <= ts <= to_ts:
                                session_credits[sid] = session_credits.get(sid, 0.0) + float(pts)
                                session_turns[sid] = session_turns.get(sid, 0) + 1
                                session_last_ts[sid] = max(session_last_ts.get(sid, 0), ts)
                    except Exception:
                        pass

                # Query session titles
                if sids:
                    placeholders = ",".join("?" for _ in sids)
                    cur.execute(f"SELECT id, title, custom_title, updated_at FROM sessions WHERE id IN ({placeholders})", sids)
                    for sid, title, custom_title, upd in cur.fetchall():
                        titles_map[sid] = custom_title or title or "未命名会话"
                        db_updated_map[sid] = upd or 0
            except Exception:
                pass
            finally:
                conn.close()

        # Build session list
        session_rows = []
        for sid in sids:
            creds = session_credits.get(sid, 0.0)
            if creds <= 0:
                continue
            turns = session_turns.get(sid, 0)
            active_sec = max(turns * 10.0, 1.0)
            rate = round(creds / active_sec, 1)
            last_ts = session_last_ts.get(sid) or db_updated_map.get(sid, 0)

            # Format timestamp
            if last_ts > 0:
                dt = datetime.fromtimestamp(last_ts / 1000.0)
                formatted_time = dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%Y-%m-%d %H:%M")
            else:
                formatted_time = "-"

            session_rows.append({
                "session_id": sid,
                "session_id_short": sid[:8],
                "title": titles_map.get(sid, "未命名会话"),
                "credits": round(creds, 1),
                "burn_rate_pts_per_sec": rate,
                "updated_at": last_ts,
                "updated_at_formatted": formatted_time,
            })

        # Sort sessions by credits desc, updated_at desc
        session_rows.sort(key=lambda x: (-x["credits"], -x["updated_at"]))

        total_creds = round(sum(s["credits"] for s in session_rows), 1)
        total_turns = sum(session_turns.get(s["session_id"], 0) for s in session_rows)
        active_sec = max(total_turns * 10.0, 1.0)
        overall_rate = round(total_creds / active_sec, 1) if total_creds > 0 else 0.0

        return {
            "window": window,
            "account": matched_alias,
            "is_active": matched_prof["is_active"],
            "uid_short": matched_prof["uid_short"],
            "total_credits": total_creds,
            "burn_rate_pts_per_sec": overall_rate,
            "session_count": len(session_rows),
            "sessions": session_rows,
        }

    @staticmethod
    def render_overview(data: Dict[str, Any], use_color: bool = True, use_unicode_bar: bool = True) -> str:
        """Human TTY layout (ABC combined micro-card + progress bars)."""
        c_bold = BOLD if use_color else ""
        c_dim = DIM if use_color else ""
        c_cyan = CYAN if use_color else ""
        c_green = GREEN if use_color else ""
        c_yellow = YELLOW if use_color else ""
        c_reset = RESET if use_color else ""

        w_name = data["window"]["name"]
        lines = []

        # 1. Header Card (Micro-card from Variant C)
        lines.append(f"{c_dim}╭─ WorkBuddy 积分总览 [{w_name}] ──────────────────────────────────────╮{c_reset}")
        lines.append(
            f"{c_dim}│{c_reset}  总消耗: {c_cyan}{c_bold}{data['total_credits']:,.1f} pts{c_reset}      "
            f"生成流速: {c_yellow}{data['burn_rate_pts_per_sec']:.1f} pts/s{c_reset}     "
            f"活跃会话: {c_bold}{data['total_sessions']}{c_reset}   {c_dim}│{c_reset}"
        )
        lines.append(f"{c_dim}╰──────────────────────────────────────────────────────────────────╯{c_reset}")
        lines.append("")

        # 2. Account Distribution Rows
        bar_width = 12
        for acc in data["accounts"]:
            active_mark = f"{c_green}* {c_reset}" if acc["is_active"] else "  "
            alias_display = sanitize_alias(acc["alias"])
            alias_fmt = f"{c_bold}{alias_display:<11}{c_reset}"

            filled = int(round((acc["ratio_pct"] / 100.0) * bar_width))
            filled = max(0, min(bar_width, filled))

            if use_unicode_bar:
                bar = f"{c_cyan}{'█' * filled}{c_dim}{'░' * (bar_width - filled)}{c_reset}"
            else:
                bar = f"{'=' * filled}{'.' * (bar_width - filled)}"

            credits_str = f"{acc['credits']:>7.1f} pts"
            ratio_raw = f"({acc['ratio_pct']:.0f}%)" if acc["ratio_pct"] == round(acc["ratio_pct"]) else f"({acc['ratio_pct']:.1f}%)"
            ratio_str = f"{c_dim}{ratio_raw:>6}{c_reset}"
            sess_str = f"{c_dim}{acc['session_count']} 会话{c_reset}"
            rate_str = f"{c_dim}[{acc['burn_rate_pts_per_sec']:.1f} pts/s]{c_reset}"

            lines.append(f"{active_mark}{alias_fmt} [{bar}]  {credits_str} {ratio_str}  {sess_str}  {rate_str}")

        return "\n".join(lines)

    @staticmethod
    def render_drilldown(data: Dict[str, Any], use_color: bool = True) -> str:
        """Renders session drill-down breakdown for a specific account."""
        c_bold = BOLD if use_color else ""
        c_dim = DIM if use_color else ""
        c_cyan = CYAN if use_color else ""
        c_yellow = YELLOW if use_color else ""
        c_reset = RESET if use_color else ""

        w_name = data["window"]["name"]
        alias = sanitize_alias(data["account"])
        total_pts = data["total_credits"]

        lines = []
        lines.append(f"{c_dim}╭─ 账号会话明细: {c_bold}{alias}{c_reset}{c_dim} [{w_name}: {c_cyan}{total_pts:.1f} pts{c_reset}{c_dim}] ───────────────────────────╮{c_reset}")
        lines.append(f"{c_dim}│  会话 ID    标题                       消耗积分    流速     更新时间  │{c_reset}")
        lines.append(f"{c_dim}├──────────────────────────────────────────────────────────────────┤{c_reset}")

        if not data["sessions"]:
            lines.append(f"{c_dim}│  (该时间窗内暂无会话产生积分消耗)                                │{c_reset}")
        else:
            for s in data["sessions"]:
                sid = s["session_id_short"]
                # Display title up to 24 chars
                raw_title = s["title"]
                if len(raw_title) > 22:
                    raw_title = raw_title[:21] + "…"
                title_pad = f"{raw_title:<22}"

                pts_str = f"{s['credits']:>7.1f} pts"
                rate_str = f"{s['burn_rate_pts_per_sec']:>4.1f}/s"
                time_str = f"{s['updated_at_formatted']:<8}"

                lines.append(f"{c_dim}│{c_reset}  {sid}  {title_pad} {c_cyan}{pts_str}{c_reset}   {c_yellow}{rate_str}{c_reset}   {time_str} {c_dim}│{c_reset}")

        lines.append(f"{c_dim}╰──────────────────────────────────────────────────────────────────╯{c_reset}")
        return "\n".join(lines)

    @staticmethod
    def to_json(data: Dict[str, Any]) -> str:
        """Strict machine-readable JSON for AI agent ingestion."""
        return json.dumps(data, indent=2, ensure_ascii=False)

    def watch_loop(
        self,
        window: Dict[str, Any],
        interval: float = 2.0,
        max_ticks: Optional[int] = None,
        account: Optional[str] = None,
        use_color: bool = True,
        use_unicode_bar: bool = True,
    ):
        """Live continuous monitor loop with instantaneous burn rate sampling."""
        c_bold = BOLD if use_color else ""
        c_cyan = CYAN if use_color else ""
        c_yellow = YELLOW if use_color else ""
        c_reset = RESET if use_color else ""

        ticks = 0
        prev_credits = 0.0
        prev_time = time.time()

        # Initial sample
        if account:
            initial_data = self.query_account_drilldown(account, window)
            prev_credits = initial_data["total_credits"]
        else:
            initial_data = self.query_usage(window)
            prev_credits = initial_data["total_credits"]

        try:
            while True:
                time.sleep(interval)
                curr_time = time.time()
                elapsed = max(curr_time - prev_time, 0.001)

                if account:
                    data = self.query_account_drilldown(account, window)
                    rendered = self.render_drilldown(data, use_color=use_color)
                else:
                    data = self.query_usage(window)
                    rendered = self.render_overview(data, use_color=use_color, use_unicode_bar=use_unicode_bar)

                curr_credits = data["total_credits"]
                delta_credits = max(curr_credits - prev_credits, 0.0)
                instant_rate = round(delta_credits / elapsed, 2)

                prev_credits = curr_credits
                prev_time = curr_time

                # Refresh screen
                if use_color and sys.stdout.isatty():
                    sys.stdout.write("\033[2J\033[H")

                time_str = datetime.now().strftime("%H:%M:%S")
                print(rendered)
                print(f"{c_bold}[{time_str}] 瞬时流速: {c_yellow}{instant_rate:.2f} pts/s{c_reset} | 采样间隔: {interval:.1f}s | 周期增量: {c_cyan}+{delta_credits:.1f} pts{c_reset} (按 Ctrl+C 退出)")
                sys.stdout.flush()

                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
        except KeyboardInterrupt:
            print("\n已安全退出实时流速监视。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workbuddy usage",
        description="WorkBuddy 积分消耗与流速统计 (wb-usage)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # Time window options
    t_group = parser.add_argument_group("时间窗筛选 (默认: 今日)")
    t_group.add_argument("--today", action="store_true", help="统计今日消耗 (00:00:00 至当前，默认)")
    t_group.add_argument("--yesterday", action="store_true", help="统计昨日全天完整 24 小时消耗")
    t_group.add_argument("--since", type=str, metavar="DURATION", help="相对时间跨度 (例如: 30m, 1h, 2h, 24h, 7d)")
    t_group.add_argument("--all", action="store_true", help="历史全量所有会话累计消耗")
    t_group.add_argument("--from", dest="from_dt", type=str, metavar="DATETIME", help="起始时间 (YYYY-MM-DD 或 YYYY-MM-DD HH:MM)")
    t_group.add_argument("--to", dest="to_dt", type=str, metavar="DATETIME", help="截止时间 (默认当前)")

    # Account drill-down
    parser.add_argument("-a", "--account", type=str, metavar="ALIAS|UID", help="下钻指定账号的各会话消耗明细表格")

    # Monitor loop
    parser.add_argument("-w", "--watch", action="store_true", help="实时持续采样监视器 (每 2 秒轮询，优雅响应 Ctrl+C)")

    # Output presentation
    p_group = parser.add_argument_group("展示格式与输出控制")
    p_group.add_argument("-j", "--json", action="store_true", help="输出 Agent 友好的纯净强类型 JSON 数据")
    p_group.add_argument("-p", "--plain", action="store_true", help="强制纯文本 ASCII 输出 (禁用 ANSI 颜色与 Unicode 进度条)")
    p_group.add_argument("-v", "--verbose", action="store_true", help="显示更多统计字段")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    meter = CreditMeter()

    try:
        window = meter.parse_time_window(
            today=args.today,
            yesterday=args.yesterday,
            since=args.since,
            all_time=args.all,
            from_str=args.from_dt,
            to_str=args.to_dt,
        )
    except ValueError as e:
        sys.stderr.write(f"错误: {e}\n")
        return 1

    # TTY and plain detection
    is_tty = sys.stdout.isatty() and not args.plain
    use_color = is_tty
    use_unicode_bar = is_tty

    # Watch loop
    if args.watch:
        meter.watch_loop(
            window=window,
            interval=2.0,
            account=args.account,
            use_color=use_color,
            use_unicode_bar=use_unicode_bar,
        )
        return 0

    try:
        if args.account:
            data = meter.query_account_drilldown(args.account, window)
            if args.json:
                print(meter.to_json(data))
            else:
                print(meter.render_drilldown(data, use_color=use_color))
        else:
            data = meter.query_usage(window)
            if args.json:
                print(meter.to_json(data))
            else:
                print(meter.render_overview(data, use_color=use_color, use_unicode_bar=use_unicode_bar))
        return 0
    except ValueError as e:
        sys.stderr.write(f"错误: {e}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"执行异常: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
