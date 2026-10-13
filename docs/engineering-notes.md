# Engineering Notes

This project grew from concrete WorkBuddy failure modes rather than from a single feature list. The README intentionally keeps the product surface short; this document explains the engineering problems that shaped the toolkit.

## 1. Account switching is a state-management problem

The obvious implementation of multi-account switching is "save one credential file per account and copy the selected file back." That is not enough.

WorkBuddy may refresh credentials while it is running. If a switcher overwrites the active file with an older Profile, it can destroy a newer token and make the saved state regress.

Toolkit therefore treats switching as a state transition:

1. inspect the current active account;
2. synchronize the newest active credential state back to the matching Profile;
3. prepare the target Profile;
4. replace the active credential atomically;
5. let WorkBuddy reload the new identity;
6. keep diagnostics available if upstream behavior changes.

The important design point is not "fast switching." It is preserving the newest known state while switching.

## 2. Workspaces did not disappear; account filtering hid them

A second failure appeared after account switching: historical sessions and workspaces looked as if they had been lost.

Investigation showed that the physical records were still present. The client filtered session visibility using the active user identity, and workspace visibility followed the visible sessions.

Toolkit therefore uses WorkBuddy's existing shared-session semantics instead of copying project data or maintaining a second workspace database.

This keeps the fix close to the real failure boundary:

```text
same local data
      │
account-scoped query
      │
      ├── account A -> visible
      └── account B -> hidden

Toolkit normalizes the session ownership path used for local sharing.
```

For the implementation, inspect `sql/` and the current `workbuddy init` behavior.

## 3. WorkBuddy 5.6+ changed credential storage

Older versions exposed important authentication fields as ordinary strings. Newer 5.6-series builds introduced encrypted credential envelopes for sensitive values.

That broke scripts which assumed that `accessToken`, `refreshToken` and related fields were always plaintext strings.

Toolkit's compatibility work follows a stricter rule:

> Do not decrypt for storage.

When runtime access to a decrypted value is required, the compatibility layer should use the WorkBuddy runtime capability and keep persistent Profiles compatible with the upstream encrypted representation. The goal is interoperability, not building a plaintext credential archive.

Because this boundary is version-sensitive, `workbuddy doctor` is more authoritative than static README claims.

## 4. Sandbox failures became an operational problem

The project later encountered failures that were not account-management problems at all: sandbox logs and session artifacts could grow aggressively, temporary output could consume large amounts of disk, and rule growth could turn into severe command latency or process termination.

Those incidents led to the log guard, bounded cleanup, sandbox healing and deeper `doctor` diagnostics.

The detailed evidence is preserved separately:

- [Incident 01 — Sandbox log and snapshot exhaustion](incidents/INCIDENT_01_SANDBOX_LOG_SNAPSHOT_EXHAUSTION.md)
- [Incident 02 — Multi-agent Cargo build cache pressure](incidents/INCIDENT_02_CARGO_MULTI_AGENT_SCCACHE.md)
- [Incident 04 — Seatbelt rule explosion and PTY freeze](incidents/INCIDENT_04_SEATBELT_RULE_EXPLOSION_AND_PTY_FREEZE.md)
- [Incident index](incidents/README.md)

The README no longer duplicates these reports. A landing page should show that the evidence exists, not reproduce the entire investigation.

## 5. Why the router and model inventory exist

Once account state and local runtime stability were under control, another failure mode became visible: an individual upstream model can be unavailable, rate-limited or temporarily unhealthy even when the WorkBuddy account itself is valid.

The optional router separates "account is healthy" from "this model request succeeded." Model inventory and usage tooling make that state observable instead of embedding model assumptions into every script.

This is an advanced capability. It is intentionally not part of the minimal installation path.

## 6. Why `doctor` is central

WorkBuddy Toolkit depends on an application it does not control. That means compatibility cannot be reduced to a version string.

`workbuddy doctor` exists to answer runtime questions such as:

- Where is the active credential store?
- What credential representation is present?
- Can the required runtime capability be resolved?
- Is the sandbox sidecar healthy?
- Has an expected upstream path or protocol drifted?

The project should prefer a visible diagnostic failure over a silent false-green state.

## 7. Engineering rules that follow from these incidents

The recurring rules are simple:

1. **Preserve upstream state before replacing it.**
2. **Do not persist decrypted secrets just because a runtime path can decrypt them.**
3. **Bound retries, file growth and IPC waits.**
4. **Use locks around repair paths that may race.**
5. **Prefer sidecar changes over patching the application bundle.**
6. **Make upstream drift observable through diagnostics.**
7. **Keep rollback possible.**

These rules are more stable than any one implementation detail.

## Further reading

- [Architecture](architecture.md)
- [Getting Started](getting-started.md)
- [Command Reference](command-reference.md)
- [ADR directory](adr/)
- [Specifications](specs/)
- [Incident Reports](incidents/README.md)
