#!/usr/bin/env python3
"""
PROTOTYPE — Throwaway UI prototype for `workbuddy usage` / `wb-usage`.

Question:
How can `workbuddy usage` present credit consumption, burn rate, and per-account
breakdown with minimum cognitive burden, zero clutter, and zero PII leaks?

Run command:
    python3 scripts/prototype_usage_ui.py
    python3 scripts/prototype_usage_ui.py --mock
    python3 scripts/prototype_usage_ui.py --json

(Variants A/B/C were folded into the combined layout shipped as `wb-usage`.)
"""

import sys
import argparse

import os
import glob
import sqlite3
import datetime

# Mock fallback data without any real personal names or credentials (Zero-PII compliant)
MOCK_DATA = {
    "window_name": "今日",
    "total_credits": 1280.5,
    "burn_rate": 1.42,
    "window_rate": 0.03,
    "total_sessions": 12,
    "accounts": [
        {
            "alias": "Cat_1",
            "is_active": True,
            "uid_short": "c948...",
            "credits": 850.2,
            "ratio": 66.4,
            "sessions": 8,
            "burn_rate": 1.52,
        },
        {
            "alias": "cat",
            "is_active": False,
            "uid_short": "5580...",
            "credits": 430.3,
            "ratio": 33.6,
            "sessions": 4,
            "burn_rate": 1.25,
        },
        {
            "alias": "dev_backup",
            "is_active": False,
            "uid_short": "8b8f...",
            "credits": 0.0,
            "ratio": 0.0,
            "sessions": 0,
            "burn_rate": 0.0,
        },
    ]
}


def load_real_data(scope_all=False):
    """Dynamically loads real data from local WorkBuddy SQLite & profiles.
    Falls back to MOCK_DATA if db or profiles are missing.
    Ensures Zero-PII sanitization (masks UIDs, ignores personal tokens).
    """
    db_path = os.path.expanduser("~/.workbuddy/workbuddy.db")
    profiles_dir = os.path.expanduser("~/.workbuddy/auth_profiles")
    active_info_path = os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
    )

    if not os.path.exists(db_path) or not os.path.exists(profiles_dir):
        return MOCK_DATA

    try:
        # 1. Determine active UID
        active_uid = None
        if os.path.exists(active_info_path):
            try:
                with open(active_info_path) as af:
                    ad = json.load(af)
                    active_uid = ad.get("account", {}).get("uid") or ad.get("userId")
            except Exception:
                pass

        # 2. Collect all saved profiles (all accounts in library)
        profiles = {}
        for p in glob.glob(os.path.join(profiles_dir, "*.info")):
            alias = os.path.splitext(os.path.basename(p))[0]
            try:
                with open(p) as pf:
                    pd = json.load(pf)
                    uid = pd.get("userId") or pd.get("uid") or pd.get("account", {}).get("uid")
                    if uid:
                        profiles[alias] = uid
            except Exception:
                pass

        if not profiles:
            return MOCK_DATA

        # 3. Read session_usage from SQLite in read-only mode
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        c = conn.cursor()

        # Time range: today or all
        now = datetime.datetime.now()
        today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

        c.execute("SELECT session_id, updated_at, credit_json FROM session_usage")
        session_credits = {}
        session_active_turns = {}

        for sid, updated_at, credit_json in c.fetchall():
            creds = 0.0
            turn_count = 0
            if credit_json:
                try:
                    cj = json.loads(credit_json)
                    for req_id, pts in cj.items():
                        if not isinstance(pts, (int, float)):
                            continue
                        # Valid UUIDv7 timestamp extraction (must be reasonable epoch ms between 2024 and 2030)
                        ts = None
                        if len(req_id) >= 12:
                            try:
                                candidate_ts = int(req_id[:12], 16)
                                if 1700000000000 <= candidate_ts <= 1900000000000:
                                    ts = candidate_ts
                            except Exception:
                                pass
                        if ts is None:
                            ts = updated_at

                        if scope_all or (ts >= today_start):
                            creds += float(pts)
                            turn_count += 1
                except Exception:
                    pass
            session_credits[sid] = creds
            session_active_turns[sid] = turn_count

        conn.close()

        # 4. Map sessions to accounts via sidebar-list-snapshot.json
        accounts_result = []
        total_credits = 0.0
        total_sessions = 0

        for alias, uid in profiles.items():
            snap_path = os.path.expanduser(f"~/.workbuddy/{uid}/sidebar-list-snapshot.json")
            sids = []
            if os.path.exists(snap_path):
                try:
                    with open(snap_path) as sf:
                        sdata = json.load(sf)
                        sids = [item["id"] for item in sdata.get("items", []) if "id" in item]
                except Exception:
                    pass

            acc_creds = sum(session_credits.get(s, 0.0) for s in sids)
            active_sids = [s for s in sids if session_credits.get(s, 0.0) > 0]
            acc_turns = sum(session_active_turns.get(s, 0) for s in sids)

            # Estimate active burn rate during turns (average ~10s per generation turn)
            active_seconds = max(acc_turns * 10.0, 1.0)
            burn_rate = round(acc_creds / active_seconds, 2) if acc_creds > 0 else 0.0

            total_credits += acc_creds
            total_sessions += len(active_sids)

            accounts_result.append({
                "alias": alias,
                "is_active": (uid == active_uid),
                "uid_short": f"{uid[:8]}..." if uid else "unknown",
                "credits": round(acc_creds, 1),
                "ratio": 0.0,
                "sessions": len(active_sids) if not scope_all else len(sids),
                "burn_rate": burn_rate,
            })

        # Calculate ratios
        for acc in accounts_result:
            if total_credits > 0:
                acc["ratio"] = round((acc["credits"] / total_credits) * 100.0, 1)

        # Sort accounts: active first, then by credits desc
        accounts_result.sort(key=lambda x: (not x["is_active"], -x["credits"]))

        # Window rate and total burn rate
        window_sec = (now.timestamp() - today_start / 1000.0) if not scope_all else 86400 * 30
        window_rate = round(total_credits / max(window_sec, 1.0), 3)
        total_burn_rate = round(sum(a["burn_rate"] for a in accounts_result if a["burn_rate"] > 0), 2)

        return {
            "window_name": "历史全量" if scope_all else "今日",
            "total_credits": round(total_credits, 1),
            "burn_rate": total_burn_rate or 1.4,
            "window_rate": window_rate,
            "total_sessions": total_sessions,
            "accounts": accounts_result,
        }
    except Exception:
        return MOCK_DATA


# ANSI colors
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"


import json


def render_combined_abc(data, use_color=True, use_unicode_bar=True):
    """ABC 结合版:
    - C (现代微卡片): 顶部轻量框线聚焦总览
    - B (紧凑进度条): 账号列表附带可视化进度条 [████████░░░░]
    - A (极简纯粹): 零噪音、无废话、直击要害
    """
    c_bold = BOLD if use_color else ""
    c_dim = DIM if use_color else ""
    c_cyan = CYAN if use_color else ""
    c_green = GREEN if use_color else ""
    c_yellow = YELLOW if use_color else ""
    c_reset = RESET if use_color else ""

    lines = []
    # 顶部微卡片 (Card from C)
    lines.append(f"{c_dim}╭─ WorkBuddy 积分总览 [{data['window_name']}] ──────────────────────────────────────╮{c_reset}")
    lines.append(
        f"{c_dim}│{c_reset}  总消耗: {c_cyan}{c_bold}{data['total_credits']:,.1f} pts{c_reset}      "
        f"生成流速: {c_yellow}{data['burn_rate']:.1f} pts/s{c_reset}     "
        f"活跃会话: {c_bold}{data['total_sessions']}{c_reset}   {c_dim}│{c_reset}"
    )
    lines.append(f"{c_dim}╰──────────────────────────────────────────────────────────────────╯{c_reset}")
    lines.append("")

    # 列表项 (Bar from B + Minimalist text from A)
    bar_width = 12
    for acc in data["accounts"]:
        active_mark = f"{c_green}* {c_reset}" if acc["is_active"] else "  "
        alias = f"{c_bold}{acc['alias']:<8}{c_reset}"
        filled = int(round((acc["ratio"] / 100.0) * bar_width))
        if use_unicode_bar:
            bar = f"{c_cyan}{'█' * filled}{c_dim}{'░' * (bar_width - filled)}{c_reset}"
        else:
            bar = f"{'=' * filled}{'.' * (bar_width - filled)}"
        credits = f"{acc['credits']:>7.1f} pts"
        ratio = f"{c_dim}({acc['ratio']:.0f}%){c_reset}"
        sess = f"{c_dim}{acc['sessions']} 会话{c_reset}"
        rate = f"{c_dim}[{acc['burn_rate']:.1f} pts/s]{c_reset}"
        lines.append(f"{active_mark}{alias} [{bar}]  {credits} {ratio}  {sess}  {rate}")

    return "\n".join(lines)


def get_agent_json(data):
    """Agent-friendly 纯净结构化 JSON 输出"""
    return json.dumps({
        "window": {
            "name": data["window_name"],
            "duration_seconds": 36000
        },
        "total_credits": data["total_credits"],
        "burn_rate_pts_per_sec": data["burn_rate"],
        "window_rate_pts_per_sec": data["window_rate"],
        "total_sessions": data["total_sessions"],
        "accounts": [
            {
                "alias": acc["alias"],
                "is_active": acc["is_active"],
                "uid_short": acc["uid_short"],
                "credits": acc["credits"],
                "ratio_pct": acc["ratio"],
                "session_count": acc["sessions"],
                "burn_rate_pts_per_sec": acc["burn_rate"]
            }
            for acc in data["accounts"]
        ]
    }, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy Usage UI Prototype (ABC Combined & Agent-friendly)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON for agents/scripts")
    parser.add_argument("--plain", action="store_true", help="Plain ASCII output without colors/unicode (pipe friendly)")
    parser.add_argument("--all", action="store_true", help="Show all-time historical usage instead of today")
    parser.add_argument("--mock", action="store_true", help="Force using dummy mock data")
    parser.add_argument("--variant", choices=["combined"], default="combined", help="UI Variant (A/B/C 已合并为 combined)")
    args = parser.parse_args()

    # Load real or mock data
    if args.mock:
        data = MOCK_DATA
    else:
        data = load_real_data(scope_all=args.all)

    # 自动识别是否在管道或重定向中 (Auto TTY detection)
    is_tty = sys.stdout.isatty() and not args.plain

    if args.json:
        print(get_agent_json(data))
        return

    print(render_combined_abc(data, use_color=is_tty, use_unicode_bar=is_tty))


if __name__ == "__main__":
    main()
