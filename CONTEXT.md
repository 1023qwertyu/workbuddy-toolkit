# WorkBuddy Credit Metering

Metering, tracking, and attribution of WorkBuddy credit consumption and burn rates across accounts and time windows.

## Language

**Credit**:
WorkBuddy's internal billing unit for AI model completions and tool executions, represented as floating-point points.
_Avoid_: Point, token, coin

**Window Burn Rate**:
The total credit consumption divided by the total duration of the specified query time window, representing overall amortized cost velocity.
_Avoid_: Flat rate, coarse speed

**Active Burn Rate**:
An *estimate* of credit consumption per second of active generation. WorkBuddy's `session_usage` records per-turn deductions but not inference duration, so the meter assumes a fixed `ASSUMED_SECONDS_PER_TURN` (10 s) per billing turn: `credits / (turns × 10 s)`. JSON output marks it with `burn_rate_is_estimate: true`; the TTY card labels it 估算流速.
_Avoid_: Peak rate, burst speed

**Instantaneous Burn Rate**:
The delta of credits consumed over a short sampling tick (e.g. 2s-5s) during live `--watch` monitoring.
_Avoid_: Realtime delta, ping rate

**Billing Turn**:
A single deduction entry tied to a unique conversation request ID within a session.
_Avoid_: Transaction, deduction event, token chunk

**Account Snapshot**:
The per-account session mapping stored at `~/.workbuddy/<uid>/sidebar-list-snapshot.json` attributing session IDs to account user IDs.
_Avoid_: User directory, sidebar state

**Account Usage**:
The aggregated credit consumption and burn rate metrics attributed to a specific account profile (identified by alias and UID).
_Avoid_: User quota, account balance

**Session Drill-down**:
Detailed per-session breakdown within a specific account, listing session title, token count, update timestamp, and credits consumed.
_Avoid_: Chat inspection, turn audit

**Default Time Window**:
The evaluation range from local midnight (`00:00:00`) of the current day to the present moment applied when no explicit time options are given.
_Avoid_: Rolling 24h, all-time default

**Time Window Filter**:
Explicit bounding parameters defining $[t_{start}, t_{end}]$ using relative durations (`--since 1h`), calendar presets (`--today`, `--yesterday`, `--all`), or explicit datetime boundaries (`--from`, `--to`).
_Avoid_: Date query, time slice

**Agent Presentation Mode**:
Machine-friendly invocation (`--json` or non-TTY piped output) producing deterministic structured data with suppressed ANSI escapes and ASCII bar fallbacks for automated agent consumption.
_Avoid_: Raw TTY output, scraped log
