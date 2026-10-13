# Feature Reference

This document is the capability index for WorkBuddy Toolkit. It keeps the README short without hiding advanced features.

The goal here is **what exists and when to use it**. Implementation details belong in [Architecture](architecture.md), [Engineering Notes](engineering-notes.md), ADRs, specs and incident reports.

## Account management

### Terminal QR login — `wb-login`

Add a WorkBuddy account without using the normal repeated desktop logout/login flow. The login path can render the QR flow in the terminal and save the resulting account state as a local Profile.

Profile handling includes collision checks so a new login is less likely to overwrite an unrelated saved account by accident.

### Account inventory — `wb-list` / `wb-status`

Inspect saved Profiles and the active account. Status tooling exposes account identity, credential representation and health information without printing raw access/refresh tokens in normal status output.

Machine-readable output is available on supported status paths; verify the exact fields with the current command help before depending on them in automation.

### Account switching — `wb-switch`

Switch between saved Profiles without repeating the full login flow.

The switch path synchronizes the active account state before replacement so a newer token refreshed by WorkBuddy is not casually overwritten by an older Profile.

### Credential health audit — `wb-audit`

Inspect saved account health and handle near-expiry / expired states through the supported refresh path. Audit behavior is throttled so health checks do not become uncontrolled network polling.

Desktop notifications are supported on applicable platforms when a saved account needs attention.

## Workspace and session continuity

### Shared local workspace/session state — `workbuddy init`

Account switching can make historical sessions appear missing because WorkBuddy scopes session visibility by account identity.

Toolkit initializes a local sharing mechanism that uses WorkBuddy's existing session semantics so multiple local Profiles can continue to see the same physical workspace/session data.

This feature does not duplicate project directories.

## Daily automation

### Check-in and continuity tasks — `wb-checkin`

Run supported daily account tasks across saved Profiles.

The command can be used manually or scheduled through platform-native mechanisms installed by the toolkit.

### Platform scheduling

Supported scheduler integrations include:

- macOS `launchd`
- Linux `systemd --user`, with a cron fallback where applicable
- Windows Task Scheduler

Background scheduling is optional. A user can keep the toolkit entirely manual.

## Usage and model tooling

### Usage / credit metering — `wb-usage`

Inspect usage and credit-flow information over supported time windows. Detailed metering behavior is specified separately in [Credit Metering Spec](spec-credit-metering.md).

### Terminal chat — `wb-chat`

Call WorkBuddy model endpoints from the terminal without using the full desktop UI for every request.

This is optional and independent from basic multi-account management.

### Model inventory — `wb-models`

Inspect model inventory and metadata that Toolkit can observe for model selection and usage decisions.

### Failover router — `wb-router`

Optional local routing layer for model-level resilience. It separates "the WorkBuddy account is valid" from "this specific upstream model request succeeded."

Use the router when you need model fallback behavior; it is not required for ordinary account switching or check-in automation.

## WorkBuddy 5.6+ credential compatibility

Newer WorkBuddy releases introduced encrypted envelopes for sensitive local credential fields. Toolkit includes a compatibility layer for environments where older plaintext assumptions no longer hold.

The design rule is:

> Runtime access to a decrypted value does not imply persistent plaintext storage.

Compatibility is runtime-sensitive. Use:

```bash
workbuddy doctor
```

instead of assuming a version number is sufficient proof.

## Diagnostics

### Compatibility doctor — `wb-doctor`

The central diagnostic command checks the local environment, credential representation, WorkBuddy runtime capabilities and selected sidecar state.

Use it after a WorkBuddy update or when behavior changes unexpectedly.

### Exit 137 / command-stall inspection

A dedicated inspection path exists for process termination and resource/sandbox failure patterns:

```bash
wb-doctor --inspect-137
```

Pair the output with [Incident Reports](incidents/README.md).

## Sandbox and runtime reliability

### Log guard — `workbuddy-log-guard`

Controls sandbox/log growth using bounded retention and size-based safeguards. The implementation is designed to avoid blindly deleting files that are still open by a running process.

### Sandbox healing — `wb-sandbox`

Repairs supported sandbox/session-state failures and keeps repair work bounded. Concurrent repair paths use coordination so multiple processes do not independently rewrite the same state at once.

### Temporary-file and update-cache cleanup

Maintenance paths cover known runaway temporary logs and stale WorkBuddy update/migration artifacts. These paths are deliberately targeted rather than acting as a generic system cleaner.

### IPC safety boundaries

Toolkit's local IPC integrations use bounded waits and payload limits so an unhealthy sidecar/runtime path does not turn into an unbounded terminal hang.

## Remote health monitoring

The repository includes GitHub Actions-based monitoring support for compatible remote runners. It can run health checks on supported platforms and use GitHub's workflow failure notification path as an alert mechanism.

This is an advanced deployment pattern. Local users do not need GitHub Actions to use the toolkit.

## Security and rollback

### Local Profile storage

Saved account Profiles remain local to the machine unless the user explicitly moves them elsewhere.

### Sensitive output boundaries

Status / JSON-oriented tooling is designed to omit raw access and refresh tokens from ordinary output.

### Process and file boundaries

Runtime helpers use restricted file/process behavior where applicable to reduce accidental credential exposure through inherited handles, permissive files or crash artifacts.

### Uninstall / rollback

Toolkit installation is designed to be reversible. Use the platform uninstall path when you want to remove CLI links, background services and Toolkit-owned integration state.

## Upstream compatibility model

There are three practical cases:

1. **WorkBuddy fixes an upstream problem itself.** Toolkit should coexist or become unnecessary for that path without requiring application-bundle patches.
2. **WorkBuddy changes unrelated features.** Existing sidecar behavior should continue if the required local contracts remain intact.
3. **WorkBuddy changes a required path, format or protocol.** Toolkit should surface the drift through diagnostics rather than silently claiming compatibility.

This is why `workbuddy doctor` is part of the normal workflow instead of only a troubleshooting command.

## Evidence and deep dives

- [Architecture](architecture.md)
- [Engineering Notes](engineering-notes.md)
- [Incident Reports](incidents/README.md)
- [ADR directory](adr/)
- [Specifications](specs/)
- [Credit Metering Spec](spec-credit-metering.md)
