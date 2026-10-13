# Architecture

WorkBuddy Toolkit is a local sidecar around the WorkBuddy desktop client. Its goal is not to replace WorkBuddy. It adds account management, automation, diagnostics, routing and runtime guards around the existing client.

## System boundary

```text
                     WorkBuddy Desktop
                           │
          ┌────────────────┼────────────────┐
          │                │                │
     Credentials         SQLite          Runtime
          │                │                │
          ▼                ▼                ▼
     Profile Store   Session Sharing   Sandbox / Logs
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                   WorkBuddy Toolkit
                           │
          ┌────────────────┼────────────────┐
          │                │                │
      Account CLI       Automation      Diagnostics
      login/list        check-in        doctor/audit
      switch/status     scheduling      incident tools
                           │
                           ▼
                    Optional Router
```

The exact paths and capabilities differ by operating system and WorkBuddy version. `workbuddy doctor` is the runtime source of truth.

## Design principles

### 1. Sidecar, not app patching

Toolkit is designed to work beside WorkBuddy rather than permanently modifying the WorkBuddy application bundle. This keeps rollback practical and reduces coupling to UI files.

### 2. Local-first credential handling

Saved account Profiles live on the user's machine. Newer WorkBuddy versions may store sensitive fields in encrypted envelopes. Toolkit's compatibility layer is designed to preserve that storage model instead of converting encrypted credentials into a plaintext archive.

### 3. Sync before switch

WorkBuddy can refresh an access token while the client is running. A naive account switch can overwrite that newer token with an older saved file.

Toolkit therefore synchronizes the active credential state back into its matching Profile before replacing the active account. Writes use temporary files and replacement semantics to reduce partial-write risk.

### 4. Reuse WorkBuddy's session semantics

One historical failure mode was that workspaces and sessions appeared to disappear after switching accounts. The data itself was still present; WorkBuddy's query path filtered sessions by account identity.

Toolkit uses WorkBuddy's existing shared-session behavior so multiple local accounts can see the same workspace/session state without copying project directories.

### 5. Bounded background work

Automation and repair paths are designed around bounded work rather than unbounded retries. Examples include:

- credential health checks with throttling;
- bounded IPC operations;
- bounded log retention;
- size limits for runaway files;
- single-flight/locking around repair paths where concurrent writes would be unsafe.

The detailed limits are implementation details and may change. Use the current source and `workbuddy doctor` when debugging a specific release.

### 6. Fail safely when upstream changes

WorkBuddy is an upstream dependency. Paths, credential formats and IPC behavior can change.

Toolkit therefore treats compatibility detection as a first-class feature. When an expected runtime capability is missing, `doctor` should make that visible rather than silently pretending the environment is healthy.

### 7. Reversible installation

Toolkit provides uninstall scripts so users can remove CLI links, background services and Toolkit-owned configuration without needing to reinstall WorkBuddy.

## Main components

| Component | Responsibility |
|---|---|
| `bin/workbuddy` | Main CLI and account/tooling entry point |
| `bin/workbuddy-log-guard` | Sandbox/log lifecycle guard |
| `install.py` / platform installers | Installation and platform integration |
| `sidecar/` | Runtime sidecar implementation |
| `scripts/` | Compatibility, maintenance and supporting tasks |
| `sql/` | Database-side support for workspace/session behavior |
| `.github/workflows/` | CI and remote monitoring workflows |
| `docs/incidents/` | Evidence-backed incident reports |

## Two reading paths

### I only want to use it

Read:

1. [Getting Started](getting-started.md)
2. [Command Reference](command-reference.md)
3. Run `workbuddy doctor` when behavior changes after a WorkBuddy update.

### I want to understand why it exists

Read:

1. [Engineering Notes](engineering-notes.md)
2. [Incident Reports](incidents/README.md)
3. `docs/adr/` and `docs/specs/` for design records and detailed specifications.

## Compatibility is a runtime property

A README cannot guarantee that every future WorkBuddy release is compatible. The practical contract is:

```bash
workbuddy doctor
```

Use that result together with the repository's current release and CI state before relying on a new upstream WorkBuddy version.
