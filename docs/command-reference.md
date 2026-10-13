# Command Reference

This page is a practical map of the main CLI surface. For exact flags supported by the current checkout, use `workbuddy help` / `wb-help` and the command's own help output.

## Command map

| Command | Purpose | Typical use |
|---|---|---|
| `workbuddy init` | Initialize Toolkit integration | First install or repair |
| `wb-login` | Add or refresh an account Profile | Add another WorkBuddy account |
| `wb-list` | List saved Profiles | See available accounts |
| `wb-status` | Inspect current account state | Verify active identity and health |
| `wb-switch` | Switch to another saved Profile | Change accounts without repeating the full login flow |
| `wb-audit` | Audit credential health | Detect expiry or refresh problems |
| `wb-checkin` | Run daily check-in / keep-alive flow | Manual or scheduled daily task |
| `wb-usage` | Inspect usage / credit flow | Understand consumption over time |
| `wb-chat` | Call WorkBuddy models from the terminal | Lightweight CLI interaction |
| `wb-router` | Manage the optional failover router | Resilient model access |
| `wb-models` | Inspect model inventory and multipliers | Model selection / cost visibility |
| `wb-doctor` | Diagnose local compatibility | First command after an upstream change |
| `wb-help` | Show installed command help | Discover current CLI surface |

Most `wb-*` commands are convenience aliases around the main `workbuddy` CLI installed by the toolkit.

## Recommended first-run sequence

```bash
workbuddy init
workbuddy doctor
workbuddy status
wb-list
```

Do not start by switching accounts repeatedly if `doctor` already reports an incompatible or partially resolved runtime.

## Account lifecycle

### Add an account

```bash
wb-login
```

Use this when you want to add a new account or refresh an existing Profile through the Toolkit login flow.

### Inspect saved accounts

```bash
wb-list
```

Use `wb-status` when you care about the currently active account and its runtime state:

```bash
wb-status
```

### Switch accounts

```bash
wb-switch
```

Switching is stateful. Toolkit synchronizes the current active credential state before replacing the active Profile so a newer token is less likely to be overwritten by stale saved state.

## Health and diagnostics

### Credential audit

```bash
wb-audit
```

Use this for account health checks, especially for Profiles that have not been activated recently.

### Compatibility doctor

```bash
wb-doctor
```

Use `doctor` after:

- upgrading WorkBuddy;
- changing machines;
- seeing unexpected logout behavior;
- seeing sandbox or IPC-related failures;
- reinstalling Toolkit.

The README intentionally does not hard-code every possible diagnostic state. The runtime output is more useful than a static compatibility promise.

### Exit 137 / command-stall investigation

The current toolkit includes a dedicated inspection path documented in the existing project material:

```bash
wb-doctor --inspect-137
```

Use it when commands are being killed or when the failure pattern points to sandbox/resource exhaustion. Pair the output with the incident reports under [`docs/incidents/`](incidents/README.md).

## Automation

### Daily check-in

```bash
wb-checkin
```

Run it manually first. Only after the command succeeds should you rely on a background schedule.

Toolkit installers can integrate with platform-native schedulers. The exact scheduler is platform-dependent; see [Getting Started](getting-started.md).

## Usage visibility

```bash
wb-usage
```

Use this to inspect usage / credit-flow information exposed by the toolkit. If you are feeding the output into another program, prefer machine-readable output when the current command supports it.

## Terminal model access

```bash
wb-chat
```

This is optional. Account management does not require you to use the CLI chat path.

## Router

```bash
wb-router
```

The router is an advanced capability for handling model-level failure separately from account health. Do not treat it as a required step in the basic install.

## Model inventory

```bash
wb-models
```

Use it to inspect the model inventory and the metadata Toolkit can observe for model selection and usage decisions.

## Machine-readable state

Some status and diagnostic paths support JSON output. The current README historically documents `--json` for status/diagnostic workflows. Verify the exact command help in your installed version before building automation around a field name.

Example pattern:

```bash
wb-status --json
```

Automation should consume documented fields and tolerate future additive fields.

## Safe operating rule

When WorkBuddy changes upstream, use this order:

```text
1. wb-doctor
2. wb-status
3. wb-list
4. only then run write/switch/automation actions
```

That keeps compatibility detection ahead of state mutation.

## Related docs

- [Getting Started](getting-started.md)
- [Architecture](architecture.md)
- [Engineering Notes](engineering-notes.md)
- [Incident Reports](incidents/README.md)
