# Global Account Selection Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the secret-safe account model, durable transaction state,
one lean per-user supervisor, isolated worker runtime, and Linux, WSL, and
macOS service lifecycle required by global Claude and Codex account
selection.

**Architecture:** Migrate the token-owning schema-version-two account store
into a stable-ID schema-version-three index plus qualified protected
credential authorities. Convert `daemon.py` into a cohesive package whose
resident process owns only a bounded peer-authenticated Unix socket, durable
scheduling, activation recovery, and a reserved Codex callback lane.
Provider-heavy work stays in hard-deadline child processes. Existing
filesystem locks remain the final cross-process authority.

**Tech Stack:** Python 3.14, Pydantic 2.13.4, Portalocker 3.2.0, Typer, Rich,
systemd user services, WSL Task Scheduler rescue, macOS LaunchAgents, pytest
9, Ruff, `ty`, `uv`, and strict JSON over local Unix-domain sockets.

## Global Constraints

- The approved design is normative:
  `docs/superpowers/specs/`
  `2026-07-23-interactive-global-account-selection-design.md`.
- This plan is phase 1 of 4. Complete it before the Codex, Claude, or
  dashboard plans.
- Never wrap, alias, replace, or redirect the vendor `claude` or `codex`
  commands. Do not edit shell startup files or `PATH`.
- Never copy a native Codex `auth.json`, manually write a Claude credential
  file, or manually write a Claude Keychain item.
- The schema-version-three account index, selected state, activation journal,
  queue, service state, socket protocol, process arguments, and logs contain
  no credential values.
- Existing setup tokens and legacy migration credentials may move only through
  a qualified, atomic CLI migration into owner-only Sidekick secret
  authorities. They are never duplicated and are deleted only after a
  provider-owned replacement is proven.
- Once any account has a managed Claude or Codex authority, preparation for
  Sidekick 0.6.0 rollback must fail before any mutation.
- The supervisor must not import provider-heavy modules, HTTP clients, Rich,
  Typer, `prompt_toolkit`, Keychain adapters, or credential schemas. The Codex
  phase may add one audited lightweight broker-wire leaf that imports only the
  standard library, core types, and strict bounded serialization.
- Every provider operation is isolated in a killable worker with a hard
  deadline. A worker receives only an operation ID in its arguments.
- The high-priority Codex callback lane never waits behind unrelated
  maintenance and can preempt a lower-priority worker holding the same Codex
  authority.
- One account failure never prevents later accounts from being attempted.
- Native Windows remains feature-disabled for account selection. Existing
  unrelated Windows package behavior remains supported.
- Automated tests use synthetic identities and fake provider, process,
  filesystem, scheduler, socket, and clock boundaries. They never touch real
  credentials or public networks.
- Use Python 3.14 native types. Add no `Any`, unjustified `cast`, blanket
  suppression, stringized annotations, unbounded input, or secret-bearing
  representation.
- Keep each module below 1000 lines and perform a cohesion split before
  approximately 800 lines. Do not add service behavior to the current
  740-line `daemon.py`, 790-line `credentials/service.py`, or 788-line
  `persistence/account_store.py`.
- Use `apply_patch` for hand-authored edits. Use owning generators for lock,
  packaging, and Homebrew output.
- Each task must leave the package installable and its currently exposed
  commands valid.
- Commit after each numbered task with the listed Conventional Commit
  message. Do not push until explicitly authorized.

---

- **Status:** Approved; not implemented
- **Date:** 2026-07-23
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Planning baseline:** `dfde7d8c3b1855e2307ed2fc24fb8a72497ed39d`
- **Upstream baseline:** `25135bbe03e51d3a3232a5171dd5c893822f4e14`
- **Current Sidekick version:** `0.7.0`
- **Supported feature platforms:** Linux, WSL, macOS arm64, macOS x64
- **Companion plans:**
  - `2026-07-23-codex-managed-auth-and-selection.md`
  - `2026-07-23-claude-managed-auth-and-selection.md`
  - `2026-07-23-interactive-account-dashboard-and-rollout.md`

## 1. Final Foundation Contract

The completed foundation provides these boundaries without implementing
provider-specific switching:

1. Every saved account has a random stable Sidekick account ID that survives
   rename, selection, migration, and restart.
2. `accounts.json` contains labels, plans, health, provider identity, and
   authority metadata, but no access, refresh, ID, or setup token.
3. Existing setup tokens and pre-managed login credentials live in separate
   owner-only per-account authorities until their provider plan replaces or
   retires them.
4. A read-only account record and a short-lived credential lease are distinct
   types. Rendering, queueing, and selection cannot obtain a lease.
5. Selected state is a provider read-back record, not a user preference.
6. Every activation is journaled through a closed legal state machine.
7. Every account has independent due and retry state.
8. A single resident supervisor accepts only same-user local requests and
   starts bounded workers.
9. Linux and macOS use one resident user service. WSL uses that same Linux
   service plus a Windows logon rescue trigger, never a second scheduler.
10. The legacy periodic scheduler is removed only after the new service is
    ready and has completed a truthful maintenance pass.

The account index uses this structural shape. Exact JSON key order is
canonical and tested.

```json
{
  "schema_version": 3,
  "accounts": {
    "75cc2b04-05ea-43d2-b897-bc960c85cd63": {
      "label": "claude-a",
      "provider_id": "claude",
      "plan": "max",
      "authority": {
        "provider_id": "claude",
        "setup_token": {
          "authority_id": "a050a4a2-357b-4923-aeed-ed5866475853",
          "expires_at": null,
          "health": "healthy"
        },
        "subscription": {
          "kind": "legacy",
          "authority_id": "671bd641-87e7-450c-91c9-04863abf3462",
          "provider_identity": null,
          "generation": null,
          "health": "migration_required"
        }
      },
      "credential_health": "migration_required"
    }
  }
}
```

The example is synthetic and abbreviated only to show envelope ownership.
The strict record fields are:

- `label`, `provider_id`, and `plan`;
- one provider-discriminated authority record;
- credential health, last refresh time, status, and safe error code;
- existing heartbeat enabled state, reset state, targets, last observation,
  status, and safe error code; and
- no metrics value, because activity snapshots remain independently owned.

A Claude authority record has optional `setup_token` and `subscription`
members. `setup_token` contains an authority ID, fixed expiry when known,
health, and observation time. `subscription` is exactly one of:

- `legacy`, containing an authority ID plus available identity and expiry
  metadata; or
- `managed`, containing an authority ID, complete provider identity, provider
  generation, verified time, executable version, and health.

A Codex authority record has exactly one `subscription` member with the same
`legacy` or `managed` distinction. A managed Codex record requires complete
provider identity and generation.

Every legacy or setup-token authority ID resolves to one owner-only secret
file whose strict schema binds schema version, authority ID, Sidekick account
ID, provider, credential kind, and the minimum credential fields for that
kind. A managed provider-owned private profile is derived from the Sidekick
account ID and has no Sidekick secret file. Secret values are absent from the
account index.

## 2. Required Implementation Order

Execute the plan suite in this order:

1. this foundation plan;
2. Codex managed authentication and selection;
3. Claude managed authentication and selection;
4. interactive dashboard and current-machine rollout.

The foundation may expose internal provider ports, but it must not claim that
switching is supported until the relevant provider capability adapter is
complete. Until then, the service reports `unsupported` before native auth
mutation.

## 3. Target Type and File Map

### 3.1 Core

Create `src/sidekick_usages/core/accounts.py` with:

- `SidekickAccountId`, a validated canonical UUID string;
- `AuthorityId`, a validated opaque identifier;
- `AuthorityGeneration`, a bounded opaque provider generation;
- `CredentialHealth`, with `healthy`, `refresh_due`, `login_required`,
  `migration_required`, `unreadable`, `malformed`, `unsupported`,
  `reconciliation_required`, and `unknown`;
- `MetricsFreshness`, with `fresh`, `stale`, and `unavailable`;
- `ClaudeSetupTokenAuthority`, containing only secret-reference presence and
  fixed-lifetime metadata;
- `ClaudeLegacyLoginAuthority`;
- `ClaudeManagedLoginAuthority`;
- `ClaudeAccountAuthority`, which permits a setup token and one subscription
  authority on the same logical account;
- `CodexLegacyAuthority`;
- `CodexManagedAuthority`;
- `SavedAccount`, the immutable no-secret account record; and
- `AuthenticatedAccount`, the worker-only combination of a saved account and
  a non-represented credential lease.

Provider identities and generations are non-represented bounded values.
They may be persisted in qualified authority and selected state, but never
rendered or logged.

Create `src/sidekick_usages/core/selection.py` with:

- `ProviderRuntimeState`;
- `ActivationPhase`;
- `ActivationOutcome`;
- `OperationKind`;
- `OperationPriority`;
- `OperationState`;
- `SelectedAccountState`;
- `ActivationRecord`;
- `DueOperation`; and
- transition functions that reject illegal activation and queue changes.

`ProviderRuntimeState` distinguishes `saved_active`, `external_active`,
`logged_out`, `unreadable`, and `unsupported`. `ActivationPhase` uses the
seven persisted names listed in Task 3. `OperationKind` distinguishes
`maintain`, `refresh`, `usage`, `activity`, `login`, `migrate`, `activate`,
`repair`, and `reconcile`. Priority order is `codex_callback`,
`interactive`, then `scheduled`.

Keep path values, Pydantic, filesystem access, process behavior, and provider
imports out of `core/`.

### 3.2 Persistence

Create these focused owners:

- `persistence/account_schema_v3.py`: strict schema-version-three codec;
- `persistence/account_index.py`: no-secret account index transactions;
- `persistence/credential_authorities.py`: protected per-account secret
  authority reads, one-time migration writes, and verified retirement;
- `persistence/selected_state.py`: provider selected-state store;
- `persistence/activation_journal.py`: activation state machine persistence;
- `persistence/operation_queue.py`: durable due/retry operations;
- `persistence/service_state.py`: service protocol and readiness observations;
- `persistence/managed_migration.py`: atomic v2-to-v3 migration;
- `persistence/managed_rollback.py`: v0.6 compatibility preflight; and
- `persistence/state_validation.py`: recursive no-secret key and value
  validation for all non-secret state.

`persistence/account_store.py` remains the public account workflow facade but
delegates version-three work to the new owners. Do not grow it past 800 lines.

### 3.3 Application paths

Extend `ApplicationPaths` in `paths.py` with qualified roots for:

- private Claude profiles;
- per-account credential authorities;
- selected state;
- activation journals;
- durable operations;
- service state;
- service logs;
- the owner-only runtime directory; and
- the supervisor socket and singleton lock.

All per-account paths are derived from `SidekickAccountId`, never from a
friendly label or raw provider identity.

### 3.4 Credential leases

Create `credentials/authorities.py` with:

- `CredentialLease`, a context-managed secret value whose representation is
  always redacted;
- `CredentialAuthorityReader`, the provider-neutral read port used only by
  workers;
- `AuthenticatedAccountResolver`, which combines one `SavedAccount` with one
  operation-scoped lease; and
- typed failures for missing, malformed, unreadable, retired, managed, and
  mismatched authorities.

Existing usage, heartbeat, refresh, and activity services must accept
`SavedAccount` plus an injected resolver. They must not retrieve credentials
from presentation or selection state.

### 3.5 Daemon package

Atomically replace `src/sidekick_usages/daemon.py` with:

- `daemon/__init__.py`: stable exports used by CLI composition;
- `daemon/models.py`: service lifecycle and safe progress results;
- `daemon/protocol.py`: bounded version-one message codec;
- `daemon/peer.py`: Linux, WSL, and macOS peer-user verification;
- `daemon/client.py`: short-lived CLI control client;
- `daemon/supervisor.py`: lean event loop and readiness owner;
- `daemon/workers.py`: worker process launch, timeout, and termination;
- `daemon/scheduler.py`: durable queue wakeup and retry dispatch;
- `daemon/recovery.py`: startup activation recovery dispatch;
- `daemon/systemd.py`: Linux and WSL user service backend;
- `daemon/launchd.py`: macOS LaunchAgent backend;
- `daemon/wsl.py`: Windows rescue-task generation and verification;
- `daemon/legacy.py`: old schedule detection and safe retirement;
- `daemon/manager.py`: public install, status, readiness, and uninstall
  facade;
- `daemon/entrypoint.py`: supervisor console entry point; and
- `daemon/worker_entrypoint.py`: provider-heavy worker console entry point.

The supervisor import graph ends at standard-library modules, `clock.py`,
`core/`, qualified persistence state modules, and the lightweight daemon
package. The worker entry point is the only daemon package module allowed to
compose `credentials/`, providers, HTTP, maintenance, heartbeat, or usage.

## 4. Task 1 — Stable Account IDs and No-Secret Schema Version Three

**Commit:** `feat(persistence): add managed account authorities`

### Tests first

- [ ] Add `tests/test_core_accounts.py` for canonical UUID validation,
  no-secret representations, valid Claude dual-authority state, invalid
  provider/authority combinations, and immutable account identity.
- [ ] Add `tests/test_persistence_account_schema_v3.py` for strict decoding,
  duplicate IDs, duplicate provider identities, unknown keys, oversized
  values, canonical encoding, and recursive token-key rejection.
- [ ] Add `tests/test_persistence_managed_migration.py` for atomic v2-to-v3
  conversion, random stable IDs through an injected ID factory, crash
  recovery, label preservation, metrics preservation, and exact source-file
  retention until commit.
- [ ] Extend `tests/test_persistence_account_store.py` for ID-based get,
  label lookup, rename without ID change, remove, reset, and provider
  filtering.
- [ ] Extend `tests/test_v060_compat.py` and
  `tests/test_persistence_rollback_coordinator.py` with:
  - a legacy/setup-token-only export that remains compatible;
  - a managed-authority preflight failure;
  - byte-for-byte proof that failure changed no artifact; and
  - proof that managed tokens are never extracted from a private authority.
- [ ] Run the new tests and confirm they fail because schema version three and
  stable account IDs do not exist:

```bash
uv run pytest \
  tests/test_core_accounts.py \
  tests/test_persistence_account_schema_v3.py \
  tests/test_persistence_managed_migration.py \
  tests/test_persistence_account_store.py \
  tests/test_v060_compat.py \
  tests/test_persistence_rollback_coordinator.py
```

### Implementation

- [ ] Add the core account types from Section 3.1. Use a canonical lowercase
  hyphenated UUID. Generate IDs only at the application or persistence
  boundary through an injected factory.
- [ ] Add strict version-three Pydantic declarations. Key the account map by
  stable ID and retain the label as mutable metadata.
- [ ] Represent Claude setup-token and subscription authorities separately so
  one logical account can own both without duplicate dashboard rows or
  metrics.
- [ ] Represent legacy Claude and Codex credentials only by an
  `AuthorityId`. Do not place their values or filesystem paths in
  `accounts.json`.
- [ ] Move each existing secret exactly once into a qualified owner-only
  per-account authority during the v2-to-v3 CLI migration. Stage all files,
  validate them, fsync them, atomically publish the index, and retain recovery
  evidence until the transaction commits.
- [ ] Preserve setup-token fixed-lifetime metadata and all current refresh,
  heartbeat, metrics, plan, and label state.
- [ ] Update `AccountStore` so public account workflows use stable IDs
  internally while accepting exact labels at current CLI boundaries.
- [ ] Enforce exact label uniqueness within one provider while allowing the
  same user-facing label for different providers. Label-only legacy commands
  must reject cross-provider ambiguity and show their provider-qualified
  equivalent. Preserve exact Unicode label behavior.
- [ ] Update rename, remove, reset, prototype import, location migration, and
  credential-ownership checks to transact the index and its referenced
  authorities together.
- [ ] Update released-v0.6 conversion so it can read legacy authorities only
  when no managed authority exists. Any managed authority rejects the whole
  conversion in preflight.
- [ ] Delete a legacy authority only after its managed provider replacement
  is committed. A failed or canceled replacement retains the original
  authority.
- [ ] Update `paths.py` and path tests for every new qualified path. Enforce
  owner-only traversal and files using the existing platform-specific
  filesystem primitives.

### Verify and commit

- [ ] Run the focused tests until green.
- [ ] Run persistence, migration, v0.6, core, and path suites:

```bash
uv run pytest \
  tests/test_core_models.py \
  tests/test_core_types.py \
  tests/test_persistence_*.py \
  tests/test_v060_*.py \
  tests/test_paths.py
```

- [ ] Run `uv run ruff check src/ tests/` and
  `uv run ty check src/ tests/`.
- [ ] Inspect tracked JSON fixtures and test output for credential-shaped
  values and real labels.
- [ ] Commit with the listed message.

## 5. Task 2 — Operation-Scoped Credential Leases

**Commit:** `refactor(credentials): isolate account credential leases`

### Tests first

- [ ] Add `tests/test_credential_authorities.py` for protected reads,
  missing/malformed/unreadable distinctions, authority retirement, and exact
  account/provider matching.
- [ ] Add `tests/test_credential_leases.py` proving:
  - secrets are absent from `repr`, `str`, exceptions, and dataclass nesting;
  - a lease is available only inside its context;
  - a closed lease cannot be reused;
  - a worker can resolve only its requested account authority; and
  - managed authority metadata cannot be mistaken for a token.
- [ ] Update provider, usage, heartbeat, activity, refresh, and credential
  tests so public services receive a `SavedAccount` plus a typed fake
  resolver.
- [ ] Add an architecture test rejecting credential-authority imports from
  `cli/`, `usage/render.py`, `daemon/supervisor.py`, and
  `daemon/protocol.py`.
- [ ] Run the focused test set and confirm failure at the old embedded-token
  `Account` boundary.

### Implementation

- [ ] Implement `CredentialLease` and the authority reader without exposing a
  token property on `SavedAccount`.
- [ ] Introduce a worker-only `AuthenticatedAccount` and change the provider
  protocol so credential-bearing methods accept that type.
- [ ] Refactor usage, refresh, heartbeat, and activity orchestration to open
  one lease at the last responsible moment and close it before returning a
  sanitized result.
- [ ] Keep refresh and heartbeat status mutations in account-index
  transactions; never return credentials merely to persist status.
- [ ] Remove durable token properties from the public saved-account model.
- [ ] Retain compatibility decoding only in the migration boundary. New
  account-index writes cannot serialize credential values.
- [ ] Update `providers/base.py`, `providers/registry.py`, and test fakes with
  exact typed ports. Do not add a generic dictionary payload.
- [ ] Add recursive secret-field guards to all non-secret persistence and
  daemon message encoders.

### Verify and commit

- [ ] Run:

```bash
uv run pytest \
  tests/test_credential_authorities.py \
  tests/test_credential_leases.py \
  tests/test_credential_output_safety.py \
  tests/test_credential_service.py \
  tests/test_usage_service.py \
  tests/test_heartbeat.py \
  tests/test_usage_activity.py \
  tests/test_provider_registry.py \
  tests/test_architecture.py
```

- [ ] Run the full provider test set to prove current behavior survives the
  boundary change.
- [ ] Run Ruff and `ty`, inspect the diff for a second credential owner, then
  commit.

## 6. Task 3 — Selected State, Activation Journal, and Durable Queue

**Commit:** `feat(persistence): add selection and operation state`

### Tests first

- [ ] Add `tests/test_selection_models.py` for each legal and illegal runtime,
  activation, and operation transition.
- [ ] Add `tests/test_selected_state.py` for one independent selected record
  per provider, strict provider read-back fields, external-active state, and
  logged-out state.
- [ ] Add `tests/test_activation_journal.py` for every interruption point:
  `prepared`, `outgoing_retained`, `target_activated`,
  `read_back_verified`, `committed`, `rolled_back`, and
  `reconciliation_required`.
- [ ] Add `tests/test_operation_queue.py` for independent account deadlines,
  retry suppression, jitter through an injected source, catch-up-once
  behavior, stable ordering, and permanent-failure reset on relevant state
  change.
- [ ] Add `tests/test_service_state.py` for protocol version, readiness,
  startup generation, clean shutdown, and sanitized failure observations.
- [ ] Add transaction tests for rename, remove, and reset while an account is
  selected, journaled, or queued.

### Implementation

- [ ] Implement the closed core state types and transition functions.
- [ ] Store selected state by provider with account ID, provider identity,
  runtime generation, verified time, and safe outcome. A label is never the
  authoritative key.
- [ ] Store one active journal per provider plus bounded terminal history.
- [ ] Enforce provider activation lock before account locks and sorted stable
  account-ID order when two authorities are needed.
- [ ] Store one due/retry record per account and operation kind. Coalesce
  duplicate due events rather than appending unbounded work.
- [ ] Store wall-clock deadlines as aware UTC and acquire monotonic deadlines
  only inside a running process.
- [ ] On account rename, preserve all ID-keyed state. On remove or reset,
  reject unsafe deletion or perform the explicit transaction required by the
  owning command.
- [ ] Enforce bounded files, strict schemas, owner-only permissions, atomic
  writes, locks, and startup recovery using existing persistence primitives.

### Verify and commit

- [ ] Run all new state tests and relevant persistence transaction suites.
- [ ] Run `uv run python packaging/check_architecture.py`.
- [ ] Run Ruff and `ty`, inspect encoded state for forbidden secret keys, then
  commit.

## 7. Task 4 — Bounded Local Protocol and Same-User Client

**Commit:** `feat(daemon): add authenticated local control protocol`

### Protocol contract

Protocol version one uses a four-byte unsigned big-endian length followed by
strict UTF-8 JSON. The maximum frame is 65,536 bytes. Every request contains:

- `protocol_version`;
- a bounded UUID `request_id`;
- one closed request kind;
- a strict kind-specific payload; and
- the client package version.

The request kinds are:

- `handshake`;
- `snapshot`;
- `subscribe`;
- `activate`;
- `refresh_account`;
- `refresh_all`;
- `reconcile`; and
- `shutdown`.

Responses and events are:

- `accepted`;
- `snapshot`;
- `progress`;
- `completed`;
- `failed`;
- `incompatible`; and
- `service_stopping`.

Account actions carry only provider and stable account ID. Friendly labels are
resolved and rendered in the CLI process. No protocol field accepts an
environment map, command path, arbitrary argv, or credential material.

### Tests first

- [ ] Add `tests/test_daemon_protocol.py` for every message variant,
  fragmented frames, multiple frames, EOF, invalid UTF-8, duplicate keys,
  unknown keys, oversized frames, invalid IDs, unsupported versions, and
  secret-shaped key rejection.
- [ ] Add `tests/test_daemon_peer.py` for Linux/WSL `SO_PEERCRED`, macOS peer
  identity, wrong user, unavailable proof, socket ownership, and feature
  disablement on native Windows.
- [ ] Add `tests/test_daemon_client.py` for connect timeout, handshake,
  progress streaming, server exit, incompatible service, and cancellation.
- [ ] Add fuzz-style bounded input cases without introducing a fuzzing runtime
  dependency.

### Implementation

- [ ] Convert `daemon.py` to the package layout in Section 3.5 in one atomic
  change. Preserve existing imported public names through
  `daemon/__init__.py`.
- [ ] Implement strict frame models and a stateful bounded decoder.
- [ ] Create the runtime directory and socket with owner-only permissions
  before listening.
- [ ] Verify the peer effective user before decoding an action payload. Fail
  closed when the operating system cannot prove it.
- [ ] Rate-limit malformed and excessive requests per connection without an
  idle polling loop.
- [ ] Make the client reject a protocol or installed-version mismatch before
  sending an activation.
- [ ] Add architecture rules that prevent provider, credential, HTTP,
  presentation, and interactive imports in the protocol and peer modules.

### Verify and commit

- [ ] Run the new daemon protocol, peer, and client tests.
- [ ] Run existing `tests/test_daemon.py` to prove the package conversion
  retained the public lifecycle contract.
- [ ] Run architecture, Ruff, and `ty` gates, then commit.

## 8. Task 5 — Lean Supervisor, Isolated Workers, and Durable Scheduling

**Commit:** `feat(daemon): supervise isolated account workers`

### Tests first

- [ ] Add `tests/test_daemon_supervisor.py` for singleton startup, readiness,
  clean stop, crash restart state, no busy loop, and connection isolation.
- [ ] Add `tests/test_daemon_workers.py` for exact executable invocation,
  operation-ID-only arguments, minimal environment, sanitized stdout,
  malformed result, timeout, graceful termination, forced kill, and
  supervisor survival.
- [ ] Add `tests/test_daemon_scheduler.py` for all-account enqueue,
  independent failure continuation, startup catch-up once, network recovery,
  jitter, permanent-failure suppression, and no duplicate dispatch.
- [ ] Add `tests/test_daemon_priority.py` proving:
  - a hung Claude worker cannot delay a Codex callback;
  - unrelated Codex maintenance cannot delay a callback;
  - same-authority lower-priority work is canceled and reaped;
  - the callback lane obtains the authority lock within its budget;
  - no lock inversion exists; and
  - a failed callback returns before Codex's external ten-second deadline.
- [ ] Add an import audit that starts the supervisor in a fresh interpreter
  and rejects provider-heavy, HTTP, Rich, Typer, `prompt_toolkit`, and
  credential modules in `sys.modules`. Before the Codex phase no provider
  module is allowed; afterward only the audited Codex broker-wire leaf is
  allowed.

### Implementation

- [ ] Add internal console scripts:

```toml
sidekick-usages = "sidekick_usages.cli:app"
sidekick-usages-supervisor = "sidekick_usages.daemon.entrypoint:main"
sidekick-usages-worker = "sidekick_usages.daemon.worker_entrypoint:main"
```

- [ ] Keep the original public entry point unchanged. The two internal entry
  points are service implementation details, not provider wrappers.
- [ ] Implement an event-driven supervisor using selectors, monotonic
  deadlines, and explicit wakeups. Do not add an asynchronous framework.
- [ ] Start workers with the exact installed worker entry point, one operation
  ID, a minimal allowlisted environment, a new process group, closed
  unrelated descriptors, bounded stdout/stderr, and no shell.
- [ ] Make workers read their strict operation record from qualified
  persistence, acquire the owning authority lock, perform one operation,
  atomically write a sanitized result, and exit.
- [ ] Keep general maintenance concurrency bounded. Reserve a separate Codex
  callback slot that does not acquire the general worker semaphore.
- [ ] Give the internal Codex callback path an eight-second total budget. If a
  lower-priority worker owns the same Codex authority, request cancellation,
  terminate it if it does not release promptly, reap it, and dispatch the
  callback worker. Return a typed failure rather than crossing the provider's
  ten-second deadline.
- [ ] Never preempt an activation after native mutation. Such work is governed
  by its activation journal and recovery path, not normal maintenance
  priority.
- [ ] Dispatch every due account even when an earlier result is permanent,
  transient, malformed, or timed out.
- [ ] Reconcile unfinished journals before declaring provider switching ready.
- [ ] Persist queue updates before acknowledging actions and result updates
  before deleting completed work.
- [ ] Implement sanitized rotating local diagnostics keyed only by account ID,
  provider, operation ID, phase, duration, version, and typed result.

### Verify and commit

- [ ] Run all daemon supervisor, worker, scheduler, priority, state, and
  architecture tests.
- [ ] Measure a test supervisor after steady state and record resident memory,
  idle CPU, imports, and worker cleanup in the test artifact.
- [ ] Require no more than 30 MiB resident memory on the documented reference
  machine. Measure the official Codex daemon separately.
- [ ] Run Ruff and `ty`, inspect child arguments and logs, then commit.

## 9. Task 6 — Linux, WSL, and macOS User-Service Lifecycle

**Commit:** `feat(daemon): install cross-platform user supervisor`

### Tests first

- [ ] Replace one-shot systemd fixtures with a `Type=simple` user service,
  restart policy, readiness checks, and no timer after transition.
- [ ] Add WSL tests proving the Windows task starts the distribution and user
  service only, carries no credentials, performs no maintenance, and is
  created for the current user without elevation.
- [ ] Add LaunchAgent tests for `RunAtLoad`, `KeepAlive`, exact executable,
  owner-only plist, login-user context, and no periodic maintenance interval.
- [ ] Add lifecycle tests for install, idempotent install, upgrade, partial
  install recovery, readiness timeout, uninstall, and preservation of
  account/provider state.
- [ ] Add transition tests proving the legacy schedule remains until the new
  supervisor is ready, queued, broker-ready when supported, maintenance-tested,
  and restart-tested.
- [ ] Add a test that fails when both legacy and new schedulers are active.

### Implementation

- [ ] Replace the existing systemd timer with a resident user service using
  the exact installed supervisor entry point and `Restart=on-failure`.
- [ ] On WSL, install the same systemd user service and generate a current-user
  Windows Task Scheduler logon rescue. The rescue starts WSL and asks the user
  manager to start Sidekick; it never calls `maintain`.
- [ ] On macOS, install one per-user LaunchAgent under
  `~/Library/LaunchAgents` in the GUI login context.
- [ ] Keep service definitions versioned and generated from typed models.
  Reject executable or path ambiguity before writing.
- [ ] Implement the transition readiness sequence exactly:
  1. install and start the resident service;
  2. complete protocol handshake;
  3. verify every saved account has due state;
  4. verify the Codex broker when Codex support is enabled;
  5. complete one bounded maintenance pass or record truthful account errors;
  6. restart and re-check the service;
  7. remove the legacy timer or task; and
  8. re-check that only one scheduler remains.
- [ ] Uninstall only the Sidekick service, socket, rescue trigger, and
  service-owned transient state. Leave accounts, private authorities,
  selected provider logins, metrics, and provider daemons untouched.
- [ ] Return a clear feature-disabled result on native Windows.

### Verify and commit

- [ ] Run `tests/test_daemon.py` and every new platform lifecycle test.
- [ ] Render each generated service artifact in a temporary directory and
  inspect exact paths, quoting, permissions, and absence of secrets.
- [ ] Run architecture, Ruff, and `ty` gates, then commit.

## 10. Task 7 — Daemon CLI, Doctor, Packaging, and Compatibility

**Commit:** `feat(cli): expose managed supervisor lifecycle`

### Tests first

- [ ] Extend `tests/test_help.py` and `tests/test_architecture.py` for the
  preserved `daemon install`, `daemon status`, and `daemon uninstall`
  commands plus service protocol/version diagnostics.
- [ ] Extend `tests/test_doctor.py` for:
  - service installed/running/ready;
  - stale service version;
  - unsupported platform;
  - socket permission or peer-proof failure;
  - queue and journal recovery state;
  - legacy scheduler conflict; and
  - provider readiness kept separate from scheduler readiness.
- [ ] Extend `tests/test_packaging.py`,
  `tests/test_homebrew_generator.py`, and wheel smoke tests for the two
  internal entry points.
- [ ] Add upgrade tests that install a previous service definition and prove
  safe replacement without touching accounts or provider state.

### Implementation

- [ ] Update the existing daemon command owner, lazy context, and help adapter
  to compose `DaemonManager` without importing the resident runtime into
  ordinary help or non-daemon commands.
- [ ] Report process, protocol, queue, journal, platform, broker, and legacy
  scheduler health independently.
- [ ] Preserve existing exit-code behavior and add no implicit installation
  to non-interactive commands.
- [ ] Add internal entry points to `pyproject.toml`, regenerate `uv.lock`, and
  update packaging smoke verification.
- [ ] Update `packaging/homebrew/generate.py`; regenerate its owned formula
  output instead of editing generated files by hand.
- [ ] Update architecture checks for the daemon package and forbidden resident
  imports.
- [ ] Document manual service lifecycle and complete uninstall behavior in
  `README.md` and the owning operational documentation. Guided first-use
  presentation remains for the dashboard plan.

### Verify and commit

- [ ] Run:

```bash
uv run pytest \
  tests/test_daemon.py \
  tests/test_doctor.py \
  tests/test_help.py \
  tests/test_architecture.py \
  tests/test_packaging.py \
  tests/test_homebrew_generator.py
uv run python packaging/check_architecture.py
uv run python packaging/smoke_wheel.py --build
```

- [ ] Run Ruff and `ty`, regenerate and inspect package metadata, then commit.

## 11. Task 8 — Foundation Integration Gate

**Commit:** `test(daemon): verify managed service foundation`

- [ ] Add one fake-provider integration test that migrates two Claude and two
  Codex accounts, starts the supervisor, queues all four, fails one worker,
  completes the remaining three, restarts, and proves no duplicate work.
- [ ] Add one crash matrix that terminates the process after every persisted
  activation phase and proves startup chooses provider read-back rather than
  journal preference.
- [ ] Add one secret-leak matrix covering account index, selection, journals,
  queue, service state, protocol frames, process arguments, stdout, stderr,
  logs, errors, and representations.
- [ ] Add one rollback matrix covering all-legacy compatibility, setup-token
  compatibility, one managed Claude authority, one managed Codex authority,
  and mixed providers.
- [ ] Add one platform contract test proving native Windows is disabled while
  Linux, WSL, macOS arm64, and macOS x64 select the correct service backend.
- [ ] Run the complete local gate:

```bash
uv run pytest --cov=sidekick_usages
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run python packaging/check_architecture.py
uv run pre-commit run --all-files
npm ci
npm audit --audit-level=moderate
npm run lint:markdown
uv build
uv run python packaging/smoke_wheel.py --build
```

- [ ] Record exact runtime measurements for supervisor memory, idle CPU,
  callback isolation, worker cleanup, and scheduler catch-up.
- [ ] Inspect `git diff --check`, module line counts, package contents,
  executable resolution, and tracked artifacts for secrets.
- [ ] Confirm this phase does not mutate the machine's real accounts,
  provider logins, or installed scheduler.
- [ ] Commit the integration evidence.

## 12. Foundation Completion Gate

Do not begin the Codex plan until all statements are true:

- [ ] Schema version three has stable IDs and a no-secret account index.
- [ ] Legacy/setup secrets are protected, referenced, and never duplicated.
- [ ] Managed-authority rollback fails before mutation.
- [ ] Rendering and selection cannot access credential leases.
- [ ] Selected state, journals, queue, and service state are strict and
  recoverable.
- [ ] The same-user socket rejects unproven peers and incompatible protocols.
- [ ] The supervisor import and memory gates pass.
- [ ] Worker timeout and crash do not terminate the supervisor.
- [ ] Codex callback capacity is reserved and lock ordering is proven.
- [ ] Linux, WSL, and macOS lifecycle artifacts pass automated tests.
- [ ] The legacy schedule transition cannot leave two schedulers active.
- [ ] Normal `claude` and `codex` executable resolution is unchanged.
- [ ] No live provider mutation has occurred.
