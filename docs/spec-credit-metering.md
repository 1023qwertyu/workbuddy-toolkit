# Spec: WorkBuddy Credit Metering & Burn Rate Tracking (`wb-usage`)

## Problem Statement

WorkBuddy users and automated coding agents manage multiple WorkBuddy accounts and run intensive LLM sessions, consuming billing credits. Currently, users have no visibility into:
1. How many credits were burned within a specific time window (e.g., today, the past hour, or across a date range).
2. The consumption velocity (burn rate in points per second, `pts/s`) during active generation.
3. The attribution of credit consumption across individual account profiles.

Without this, users face "credit anxiety", cannot detect abnormal runaways or rogue sessions, and agents cannot make informed autonomous decisions about when to switch accounts based on quota velocity.

## Solution

A high-cohesion, low-cognitive-load CLI command `workbuddy usage` (aliased as `wb-usage`) backed by an isolated deep module `sidecar/credit_meter.py`. It provides:
- A minimalist human-facing visual card with ASCII/Unicode progress bars (`[████████░░░░]`) and zero-PII sanitization.
- A deterministic, machine-readable `--json` format for AI Agent tool calls and autonomous account governance.
- Flexible time-window filtering (`--today`, `--since <1h|30m|7d>`, `--yesterday`, `--all`, `--from/--to`).
- Account-level filtering and session drill-down (`--account <alias>`).
- Real-time continuous sampling monitor (`--watch`).

---

## User Stories

1. As a developer, I want to run `wb-usage` without arguments, so that I can see today's total credit burn, active burn rate, and account distribution in under a second with zero cognitive clutter.
2. As a developer, I want to run `wb-usage --since 1h`, so that I can immediately check how many points my latest automated coding run consumed and at what speed.
3. As a developer, I want to run `wb-usage --all`, so that I can see the lifetime historical credit consumption across all my saved account profiles.
4. As a developer, I want to run `wb-usage --account Cat_1`, so that I can drill down into the specific chat sessions of that account and see which conversations consumed the most points.
5. As a developer, I want to run `wb-usage --watch`, so that I can observe the real-time instantaneous burn rate while WorkBuddy is executing heavy completions.
6. As an autonomous AI agent, I want to run `wb-usage --json`, so that I can parse clean, typed numerical metrics (floats, timestamps, account dicts) without regexing terminal escape codes.
7. As an automated script in a CI pipeline, I want piped invocations (`wb-usage | cat`) to automatically suppress ANSI color codes and fallback to ASCII bars (`[====....]`), so that text processing never breaks.
8. As a privacy-conscious developer, I want the CLI to strictly omit my real Chinese name, telephone number, or sensitive tokens, so that terminal recordings or public logs remain 100% Zero-PII compliant.
9. As a developer with newly added accounts that have 0 usage, I want them clearly displayed with `0.0 pts (0%)`, so that I know they are healthy and ready without confusion.
10. As a developer running WorkBuddy in parallel, I want `wb-usage` to connect to SQLite in read-only mode (`mode=ro`), so that concurrent writes from WorkBuddy never suffer database lock errors.

---

## Parameter Specification

| Flag / Parameter | Type | Default | Description |
|---|---|---|---|
| *(none)* | - | Today | Default evaluation range from local midnight (`00:00:00`) to present |
| `--today` | flag | true | Explicitly evaluate today's window (`00:00:00` to present) |
| `--yesterday` | flag | false | Evaluate yesterday's full 24-hour day (`00:00:00` to `23:59:59`) |
| `--since <DURATION>` | string | - | Relative window back from now (e.g., `30m`, `1h`, `2h`, `24h`, `7d`) |
| `--all` | flag | false | All-time lifetime historical consumption across all sessions |
| `--from <DATETIME>` | string | - | Explicit start timestamp (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM`) |
| `--to <DATETIME>` | string | now | Explicit end timestamp (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM`) |
| `--account <ALIAS\|UID>` / `-a` | string | - | Filter by single account and drill down into session-level breakdown |
| `--watch` / `-w` | flag | false | Live monitoring loop refreshing every 2s with instantaneous burn rate |
| `--json` | flag | false | Output strict machine-readable JSON for agents and tools |
| `--plain` | flag | auto | Suppress ANSI colors and use ASCII progress bars (`[====....]`) |
| `--verbose` / `-v` | flag | false | Show advanced statistical fields (window amortized rate, turn counts) |

---

## Data Schema & Output Specification

### 1. Header Summary Card (Human TTY)
```text
╭─ WorkBuddy 积分总览 [今日] ──────────────────────────────────────╮
│  总消耗: 22.4 pts      生成流速: 1.1 pts/s     活跃会话: 1   │
╰──────────────────────────────────────────────────────────────────╯
```
- `总消耗`: Total credits deducted in the time window (1 decimal place).
- `生成流速`: Active generation burn rate during inference (`pts/s`).
- `活跃会话`: Count of unique sessions updated within the window.

### 2. Account Distribution Rows
```text
* Cat_1       [████████████]     22.4 pts (100%)  1 会话  [1.1 pts/s]
  dev_backup  [░░░░░░░░░░░░]      0.0 pts (0%)    0 会话  [0.0 pts/s]
  cat         [░░░░░░░░░░░░]      0.0 pts (0%)    0 会话  [0.0 pts/s]
```
- `*`: Active account indicator.
- `Alias`: Clean profile alias name (left-aligned).
- `Bar`: 12-char graphical bar (`█` filled, `░` empty).
- `Credits`: Points consumed in window.
- `Ratio`: Percentage of total consumption.
- `Sessions`: Number of active sessions for this account.
- `Rate`: Account-specific generation burn rate.

### 3. Session Drill-down (`--account <alias>`)
```text
╭─ 账号会话明细: Cat_1 [今日: 22.4 pts] ───────────────────────────╮
│  会话 ID    标题                       消耗积分    流速     更新时间  │
├──────────────────────────────────────────────────────────────────┤
│  68dd2739  修复支付网关接口边界        22.4 pts   1.1/s   13:10:45 │
╰──────────────────────────────────────────────────────────────────╯
```

### 4. Agent Machine-Readable Schema (`--json`)
```json
{
  "window": {
    "type": "today",
    "name": "今日",
    "from_timestamp": 1791820800000,
    "to_timestamp": 1791868277153,
    "duration_seconds": 47477
  },
  "total_credits": 22.4,
  "burn_rate_pts_per_sec": 1.1,
  "window_rate_pts_per_sec": 0.0005,
  "total_sessions": 1,
  "active_account": "Cat_1",
  "accounts": [
    {
      "alias": "Cat_1",
      "is_active": true,
      "uid_short": "c9485804...",
      "credits": 22.4,
      "ratio_pct": 100.0,
      "session_count": 1,
      "burn_rate_pts_per_sec": 1.1
    },
    {
      "alias": "dev_backup",
      "is_active": false,
      "uid_short": "8b8f8495...",
      "credits": 0.0,
      "ratio_pct": 0.0,
      "session_count": 0,
      "burn_rate_pts_per_sec": 0.0
    }
  ]
}
```

---

## Implementation Decisions

1. **Architecture & Seam (ADR-0002)**:
   - Module `sidecar/credit_meter.py`: Class `CreditMeter` encapsulates SQLite access, time range slicing, UUIDv7 parsing, and metrics calculation.
   - Entrypoint `bin/workbuddy`: Adds dispatch for `usage` and registers global aliases `wb-usage` / `workbuddy-usage`.
   - Passes the Deletion Test: Removing `sidecar/credit_meter.py` and the CLI hook completely deletes the feature with 0 regressions to authentication or daemon features.
2. **Turn-Level Timestamp Attribution (ADR-0001)**:
   - Parses turn timestamps from `requestId` (UUIDv7 hex epoch milliseconds with `1700000000000 <= ts <= 1900000000000` sanity check), falling back to `session_usage.updated_at`.
3. **Safe Concurrency (ADR-0003)**:
   - SQLite opened via `file:{path}?mode=ro`, `uri=True` with a 5.0s busy timeout.
4. **Agent-Friendly Dual Mode (ADR-0004)**:
   - Automated TTY detection (`sys.stdout.isatty()`) suppressing ANSI escapes and Unicode blocks when piped or when `--plain` / `--json` is active.

---

## Testing Decisions

- **Test Suite**: `tests/test_credit_meter.py`.
- **Scope**:
  - Test time window parsers (`today`, `yesterday`, `since 1h`, `from/to`).
  - Test UUIDv7 timestamp extraction and fallback behavior.
  - Test account snapshot mapping with 0-usage accounts.
  - Test JSON structure schema compliance.
  - Test ANSI suppression and ASCII fallback on non-TTY pipes.
  - Test drill-down filtering by account.
  - Test read-only SQLite concurrency behavior.

## Out of Scope

- Modifying or clearing WorkBuddy's credit balance (read-only query engine).
- Sending credit exhaustion alerts via external SMS or paid webhook.
