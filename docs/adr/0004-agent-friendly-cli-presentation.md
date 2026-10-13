# Agent-Friendly Dual Presentation for Credit Metering

To eliminate human cognitive overhead while ensuring seamless AI agent CLI delegation, `workbuddy usage` adopts a unified dual-mode presentation architecture:
1. **Human TTY Mode (ABC Combined)**: A micro-card header summarizing total credits and active generation burn rate, followed by a compact visual progress bar (`[████████░░░░]`) and per-account metrics with zero PII.
2. **Machine/Agent Mode (`--json` & Auto-TTY)**: A strictly-typed, machine-readable JSON structure for LLMs and script automation. When stdout is piped or `--plain` is passed, ANSI color escapes are automatically suppressed and Unicode bars fall back to ASCII (`[====....]`).
