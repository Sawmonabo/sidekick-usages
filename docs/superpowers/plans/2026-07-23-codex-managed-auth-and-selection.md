# Codex Managed Authentication and Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every saved Codex account fresh in its own independently
authenticated private `CODEX_HOME`, then project the selected account into
Codex's official shared daemon so normal and already-connected supported
`codex` sessions adopt it without a wrapper.

**Architecture:** Use the official Codex app-server as the only durable
credential writer. Private workers perform strict JSON-RPC login, account
read, forced refresh, and generation verification inside one stable home per
account. The resident Sidekick supervisor capability-gates the official
shared daemon, injects only an ephemeral selected account through the
version-pinned `chatgptAuthTokens` method, and remains the singleton responder
for external refresh requests.

**Tech Stack:** Python 3.14, the foundation supervisor and persistence ports,
official Codex app-server JSON-RPC, official Codex app-server daemon Unix
socket, `websockets` 16.1.1, Pydantic 2.13.4, Portalocker 3.2.0, pytest 9,
Ruff, `ty`, and the release-matched Codex CLI schema.

## Global Constraints

- Complete
  `2026-07-23-global-account-selection-foundation.md` first.
- The approved design and tracked research are normative. Revalidate the
  installed binary and current release-matched upstream source before
  implementing an unstable method.
- At planning time the inspected executable is `~/.local/bin/codex`, version
  `0.145.0`.
- Sidekick never POSTs to Codex's private OAuth endpoint.
- Sidekick never imports, copies, edits, replaces, persists, adopts, or
  refreshes credentials from the native default `~/.codex/auth.json`.
  External-login reconciliation may read native authentication only long
  enough to derive a non-secret identity and generation observation, then
  immediately discards it.
- Each saved account is authenticated independently in its final private
  `CODEX_HOME`.
- Managed Codex inside that home is the sole durable writer. `accounts.json`
  stores only authority metadata.
- A forced refresh is successful only when provider identity is unchanged and
  protected generation advances.
- The selected account's access token and account ID exist in Sidekick memory
  only long enough to answer the official daemon. They are never persisted,
  logged, rendered, placed in arguments, or sent over the dashboard socket.
- `chatgptAuthTokens` and its refresh request are unstable internal Codex
  contracts. Exact version and generated-schema capability checks must pass
  before any shared-runtime auth mutation.
- Codex 0.145.0 does not return its active ChatGPT account ID through
  `account/read` or `account/updated`. Activation therefore proves the token
  claim, auth-file identity, and saved identity agree before sending; then
  requires the exact correlated install response, external-auth update, and
  explicit account readiness read. Do not describe this as an independent
  daemon account-ID read-back.
- A capability failure disables Codex switching before touching native auth.
  There is no fallback to `auth.json`, direct OAuth, `codex exec`, or copied
  credentials.
- The Sidekick supervisor has one Codex daemon connection and one refresh
  responder. Dashboard and TUI connections never race to answer refresh.
- Codex 0.145.0 broadcasts an external-token refresh request to every daemon
  client and accepts the first response. Official TUIs do not answer it.
  Sidekick guarantees exactly one Sidekick responder, but cannot exclude a
  custom same-user daemon client from racing that response.
- `codex exec`, native Windows, pre-daemon embedded TUIs, and launch
  configurations that bypass daemon reuse remain accurately unsupported.
- Pre-daemon TUIs require one restart after enrollment. Later supported TUIs
  update on their next safe authenticated request. In-flight requests are
  never retargeted.
- A daemon disconnect is fatal to attached Codex 0.145.0 TUIs. Sidekick
  reconnects and rehydrates after daemon replacement, but affected TUIs must
  be restarted. Routine switching never restarts the daemon.
- Automated tests use fake binaries, app servers, sockets, clocks, and
  credential homes. They never use a real Codex account or public network.
- Follow the foundation plan's lean-test contract. Reuse or replace existing
  Codex tests, default to at most two new coherent behavior tests per task,
  and add a third only for a separate security or crash-recovery invariant.
- Prove behavior once through the highest stable Codex boundary. Do not add a
  test per JSON-RPC message, phase, error, field, launch mode, or state
  permutation; use only representative fail-closed cases with distinct
  production branches.
- The Codex phase gate maps existing task evidence and adds no duplicate
  end-to-end, secret-matrix, or unsupported-surface suite.
- The Codex phase may add only
  `tests/test_codex_managed_runtime.py`; all other assertions extend existing
  Codex owner tests. This is a ceiling, not a target.
- Do not add a JSON-RPC dependency. The consumed protocol is narrow, strict,
  bounded, and release-gated.
- Use the pinned maintained `websockets` transport for the official
  WebSocket-over-Unix-socket boundary. It supports Python 3.14, has no runtime
  dependencies, and avoids a second security-sensitive RFC 6455
  implementation. Connect only to `/rpc`, disable frame-payload logging, and
  bound writes independently of the receiver thread. JSON-RPC decoding,
  correlation, and release-specific `emittedAtMs` validation remain
  Sidekick-owned.
- Keep provider-specific code in `providers/codex/`, transaction policy in
  `credentials/`, and qualified state in `persistence/`.
- Commit and push after each numbered task with the listed message under the
  current explicit authorization.

---

- **Status:** Complete; Claude managed authentication is next
- **Date:** 2026-07-23
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Planning baseline:** `dfde7d8c3b1855e2307ed2fc24fb8a72497ed39d`
- **Installed Codex baseline:** `codex-cli 0.145.0`
- **Required platforms:** Linux, WSL, macOS arm64, macOS x64
- **Previous phase:** `2026-07-23-global-account-selection-foundation.md`
- **Next phase:** `2026-07-23-claude-managed-auth-and-selection.md`

## 1. Final Codex Contract

For saved accounts A and B:

```text
private CODEX_HOME A -> official managed Codex auth A
private CODEX_HOME B -> official managed Codex auth B
                                  |
                     selected fresh projection
                                  |
                     official shared Codex daemon
                                  |
                  supported ordinary Codex TUIs
```

Maintenance always evaluates both private homes. Selection only changes the
ephemeral projection in the shared daemon.

A managed authority record contains:

- stable Sidekick account ID;
- stable private-home authority ID;
- verified Codex account ID;
- Codex executable provenance and version;
- protected credential generation;
- last official read and refresh times;
- credential health and action-required state; and
- no token or default-home path.

The provider adapter exposes these typed operations:

- discover and prove one Codex executable;
- inspect generated app-server capabilities;
- start an initialized bounded private app-server session;
- read one private account without refresh;
- force one private account refresh;
- start and observe official login in one final private home;
- manage and connect to the default official daemon;
- install one external runtime account;
- corroborate one external runtime projection without inventing an unavailable
  daemon account-ID read-back;
- receive and answer an external refresh request; and
- report unsupported session surfaces.

## 2. Target File Map

Create focused modules rather than expanding the current 615-line `auth.py`,
536-line `credentials/codex/coordinator.py`, or 427-line
`auth_migration.py`:

- `providers/codex/app_server/executable.py`: exact executable and version
  discovery;
- `providers/codex/app_server/capabilities.py`: generated-schema and version
  gate;
- `providers/codex/app_server/jsonrpc.py`: bounded JSON-lines framing and
  correlation;
- `providers/codex/app_server/session.py`: private managed app-server
  lifecycle;
- `providers/codex/account.py`: strict account read and generation snapshots;
- `providers/codex/login.py`: final-home official login workflow;
- `providers/codex/broker/daemon.py`: official daemon lifecycle and socket
  discovery;
- `providers/codex/broker/wire.py`: lightweight bounded daemon messages;
- `providers/codex/broker/external_auth.py`: strict unstable external-auth
  messages;
- `providers/codex/broker/service.py`: provider-specific callback validation;
- `providers/codex/session_support.py`: supported/unsupported launch status;
- `credentials/codex/authorities.py`: managed-home orchestration;
- `credentials/codex/activation.py`: provider activation transaction;
- `credentials/codex/reconciliation.py`: native external-choice policy; and
- `persistence/credentials/codex.py`: no-secret managed metadata transaction.

Keep `providers/codex/auth.py` as the protected credential-envelope reader and
official auth subprocess boundary. Remove obsolete responsibilities after
callers move; leave no second implementation.

## 3. Task 1 — Exact Executable, Schema, and JSON-RPC Boundary

**Commit:** `feat(codex): add versioned app-server boundary`

### Tests first

- [x] Add one supported-boundary scenario to the phase's sole approved
  `tests/test_codex_managed_runtime.py` owner. Cover exact executable
  provenance, generated-schema capability, JSON-RPC initialization, one
  request/notification exchange, and bounded redacted shutdown without
  pushing the near-limit provider test module past its cohesion threshold.
- [x] Add one fail-closed scenario using a synthetic executable whose schema
  lacks one required auth capability and whose response is malformed. Prove
  failure occurs before a managed worker or shared-runtime mutation starts.
- [x] Keep both scenarios on a small fake executable. Do not create separate
  executable, schema, and JSON-RPC permutation suites or invoke the installed
  binary from automated tests.
- [x] Run the focused tests and confirm failure because the versioned
  app-server boundary does not exist.

### Implementation

- [x] Resolve Codex once through `shutil.which`, require an absolute regular
  executable, obtain `codex --version`, and retain immutable provenance for
  the operation.
- [x] Generate the experimental JSON schema in a qualified temporary
  directory using:

```bash
SIDEKICK_CODEX_SCHEMA_DIR=$(mktemp -d)
codex app-server generate-json-schema \
  --experimental \
  --out "$SIDEKICK_CODEX_SCHEMA_DIR"
```

- [x] Parse only the generated files required for managed `account/read`,
  official login, `chatgptAuthTokens`, `account/updated`, and
  `account/chatgptAuthTokens/refresh`.
- [x] Hash the capability schema only for local compatibility-cache
  invalidation. Never log or confuse that schema hash with a credential
  generation.
- [x] Pin compatibility to the exact major/minor/patch and required schema
  shapes. A new installed version is unsupported until the probe passes.
- [x] Implement a bounded JSON-RPC session over stdio with:
  - 1 MiB maximum line;
  - strict duplicate-key rejection;
  - monotonically allocated request IDs;
  - explicit initialize then initialized ordering;
  - separate response, notification, and server-request types;
  - monotonic deadlines; and
  - forced child termination and reaping on failure.
- [x] Use a minimal child environment with the requested private
  `CODEX_HOME`. Do not inherit credential environment variables.
- [x] Redact provider output at the subprocess boundary before raising typed
  errors.
- [x] Update architecture checks so generic JSON-RPC framing does not import
  credentials, HTTP, CLI, or presentation modules.

### Verify and commit

- [x] Run:

```bash
uv run pytest \
  tests/providers/codex/test_provider.py \
  tests/providers/codex/test_app_server.py \
  tests/test_architecture.py
```

- [x] Run Ruff and `ty`, inspect every subprocess call for absolute argv and
  bounded output, then commit.

**Evidence reconciliation, 2026-07-26:** Task 1 was committed in `3f59132`.
Its current focused owners are `tests/providers/codex/test_provider.py` and
`tests/providers/codex/test_app_server.py`; the complete Codex phase gate was
closed in `c8b4780`.

## 4. Task 1A — Repository Namespace Cohesion

**Commit:** `refactor(architecture): organize owner namespaces`

### Existing behavior

- [x] Use the existing provider, credential, account-store, private-tree,
  managed-service, native-filesystem, daemon, usage, CLI, packaging, and
  architecture suites as the load-bearing baseline.
- [x] Add no new behavior tests for file moves. A test that can fail only
  because an internal module path changed is not a product test.

### Implementation

- [x] Replace repeated flat filename families with direct owner packages
  across the complete production tree:
  - `credentials/{claude,codex}/` for provider-specific coordination;
  - `daemon/{control,runtime,worker}/` for resident control, supervisor, and
    isolated-worker boundaries;
  - `doctor/` for diagnostics and credential classification;
  - `providers/codex/app_server/` for the exact executable, capability,
    process, JSON-RPC, and initialized-session boundary;
  - `providers/claude/schema/` for credential and usage schemas;
  - `usage/presentation/` for Rich and narrow rendering;
  - `persistence/accounts/` for the index, runtime bridge, and store;
  - `persistence/credentials/` and
    `persistence/credentials/{refresh,transactions}/` for credential
    authority, refresh, repository, planning, commit, and recovery;
  - `persistence/private/` and `persistence/private/bundles/` for private
    filesystem, credential tree, ports, paths, references, and writes;
  - `persistence/filesystem/` for qualified access and transactions;
  - `persistence/schema/refresh/` for refresh journals and stages;
  - `persistence/state/` for shared strict non-secret state mechanics;
  - `persistence/supervisor/` for activation, selected state, operation
    authority, operation queue, service state, and worker results;
  - `persistence/platform/{macos,posix,windows}/` for native adapters; and
  - nested native `private/` packages where tree, bundle, and adapter modules
    form another coherent family.
- [x] Move implementations rather than re-exporting them. Delete the old flat
  modules and update every caller to import the owning module directly.
- [x] Keep package `__init__.py` files registration-free and free of
  compatibility facades.
- [x] Keep constants, global aliases, and `__all__` directly below imports.
- [x] Extend the architecture gate across every production folder. Reject
  retired flat namespaces and any new unreviewed repeated prefix or suffix
  family.

### Verify and commit

- [x] Prove no retired module or import remains with `rg`.
- [x] Run the existing focused persistence, credential, managed-service,
  native-filesystem, CLI, packaging, and architecture suites.
- [x] Run Ruff, `ty`, the full suite, and pre-commit, then commit and push.

## 5. Task 1B — Repository Module Conventions

**Commit:** `refactor(architecture): enforce module conventions`

### Existing behavior

- [x] Add no product tests. Use the existing architecture mutation table and
  complete suite to prove this source-shape-only section.

### Implementation

- [x] Apply every approved module convention to all production folders:
  static top-level imports only, no import aliases, declaration blocks
  directly after imports, and no late constants, globals, aliases, or
  `__all__`.
- [x] Keep models, schemas, and types in an owner-local designated module or
  package. Split mixed modules when a declaration cannot live in the top
  declaration block without depending on a later implementation.
- [x] Remove compatibility re-export facades and import implementations
  directly from their owners.
- [x] Add repository-wide architecture rules for these conventions without
  per-file allowlists or blanket suppressions.

### Verify and commit

- [x] Prove no function-local import, import alias, late declaration,
  misplaced model/schema/type module, or compatibility facade remains.
- [x] Run Ruff, `ty`, the architecture mutation table, the full suite, and
  pre-commit, then commit and push.

## 6. Task 2 — Managed Private-Home Read and Refresh

**Commit:** `feat(codex): refresh managed private authorities`

### Tests first

- [x] Extend `tests/test_credential_refresh_codex.py` with one two-home
  scenario: both accounts are read without refresh, each is forced through
  `account/read` with `refreshToken: true`, identity stays fixed, generation
  advances, homes remain independent, and only sanitized metadata persists.
- [x] Add one focused fail-closed table containing only distinct trust
  failures: wrong identity, non-advanced generation, and malformed protected
  state. Prove the prior authority remains unchanged and no token reaches a
  result or index.
- [x] Do not add separate lifecycle, serialization, containment, and lock
  suites; assert those boundaries in the two managed-account scenarios.

### Implementation

- [x] Derive each private `CODEX_HOME` from the stable account ID through
  `paths.py`. Never accept a user-supplied home for a managed authority.
- [x] Require official file-backed Codex auth storage in private homes.
- [x] Snapshot the protected auth envelope before and after the app-server
  operation using the existing strict reader. Keep token values
  non-represented and inside the worker.
- [x] Implement read-only state with `account/read` and
  `refreshToken: false`.
- [x] Implement forced refresh with:

```json
{
  "method": "account/read",
  "params": {
    "refreshToken": true
  }
}
```

- [x] Treat the app-server response as necessary but insufficient. Success
  requires:
  - a non-null account;
  - expected stable provider identity;
  - valid protected post-state;
  - the same identity before and after; and
  - an advanced provider-owned generation for forced refresh.
- [x] Return strict sanitized outcomes for healthy, unchanged, rejected,
  logged out, incompatible, malformed, timeout, and transient states.
- [x] Persist only generation, identity, version, timestamps, health, and
  action-required metadata.
- [x] Use the qualified authority lock shared by scheduled refresh, broker
  refresh, migration, and activation.

### Verify and commit

- [x] Run the two managed-account scenarios plus existing Codex provider,
  credential-output, and persistence regressions they touch.
- [x] Run Ruff, `ty`, and architecture checks.
- [x] Confirm no direct network request exists in the new managed path, then
  commit.

## 7. Task 3 — Independent Final-Home Login and Legacy Migration

**Commit:** `feat(codex): migrate accounts to independent managed homes`

### Tests first

- [x] Extend `tests/test_cli_provider_credentials.py` with one two-account
  migration scenario: each account logs in officially inside its final home,
  one cancellation or identity mismatch retains its legacy authority without
  blocking the other, and retry commits metadata before retiring legacy
  state while preserving label, plan, heartbeat, and metrics.
- [x] Add one interruption scenario after official login but before metadata
  commit. Recovery must verify the final home and either finish the
  same-identity commit or require reconciliation; it must never import or
  copy native `auth.json`.
- [x] Fold private-home uniqueness and no-copy assertions into these tests
  rather than adding standalone cases.

### Implementation

- [x] Allocate the final stable home before login and authenticate it in place.
  Do not use a disposable source directory.
- [x] Prefer app-server `account/login/start` and its completion
  notifications. Use a narrowly controlled `codex login` child only if the
  exact supported binary lacks the required managed login method and the
  approved design is formally updated first.
- [x] Surface the provider URL or device step through a sanitized worker event.
  Never put provider credentials in the event.
- [x] Allow only unavoidable user browser, MFA, password, or consent input.
- [x] Verify the final provider identity against the saved logical account
  before converting authority metadata.
- [x] Record the managed authority transaction, prove official refresh, then
  retire the legacy duplicated credential. Preserve it on every unsuccessful
  path.
- [x] Migrate accounts independently and continue after account-scoped manual
  action.
- [x] Update `codex login` and repair command workflows to use final managed
  homes. Remove any option that treats the active native login as a source for
  managed accounts.
- [x] Keep the default native home unchanged throughout migration.

### Verify and commit

- [x] Run the two auth-migration scenarios plus existing persistence, CLI,
  and output-safety regressions they touch.
- [x] Run Ruff and `ty`.
- [x] Inspect fake subprocess events and staged artifacts for token
  duplication, then commit.

## 8. Task 4 — Official Shared-Daemon Lifecycle and Read-Back

**Commit:** `feat(codex): manage official shared daemon`

### Tests first

- [x] In `tests/test_codex_managed_runtime.py`, add one shared-runtime
  integration test covering idempotent official daemon startup, readiness,
  selected-account installation, provider readiness read-back, two connected
  fake TUI observers, broker reconnect and rehydration after daemon
  replacement, and no write to default `auth.json`. Do not claim that TUIs
  attached to the replaced daemon reconnect.
- [x] In the same file, add one preflight rejection test for incompatible
  version, schema, or socket ownership. Use a small table because these are
  distinct trust authorities, and prove none reaches external account
  installation.
- [x] Do not create separate daemon-lifecycle and external-auth test files.

### Implementation

- [x] Manage the official daemon with the resolved executable:

```bash
codex app-server daemon start
codex app-server daemon version
```

- [x] Connect through the official default Unix control socket only after
  verifying owner, type, version, and readiness.
- [x] Keep daemon lifecycle distinct from the Sidekick supervisor lifecycle.
  Sidekick may start and reconnect to it but does not replace its executable or
  socket.
- [x] Perform a fresh generated-schema capability probe before first external
  auth installation after either process version changes.
- [x] Expose a lock-assuming, worker-compatible selected-account lease and
  construct the exact release-matched `chatgptAuthTokens` request. Task 5
  owns high-priority worker dispatch.
- [x] Send the ephemeral access token and account ID only to the official
  daemon connection. Do not write either to default `auth.json`.
- [x] Before sending, require the token claim, managed auth-file account ID,
  and saved provider identity to match. Then require the exact correlated
  `chatgptAuthTokens` response, `account/updated` in external-auth mode, and
  explicit non-null ChatGPT `account/read` before returning a
  correlated-ready projection receipt. Do not persist or report this as an
  independent daemon account-ID verification.
- [x] Mark the provider not ready on daemon restart until the selected
  projection is rehydrated and verified.
- [x] Detect daemon-connected support separately from `codex exec`, embedded
  pre-daemon TUIs, native Windows, and launch modes that bypass daemon reuse.

### Verify and commit

- [x] Run the two shared-runtime scenarios.
- [x] Run the daemon priority and import audits from the foundation.
- [x] Run Ruff, `ty`, and architecture checks, then commit.

## 9. Task 5 — Singleton Refresh Broker

**Commit:** `feat(codex): answer shared-daemon refresh requests`

### Tests first

- [x] Extend `tests/test_codex_managed_runtime.py` with one broker lifecycle
  scenario: exactly one responder routes the selected identity, forces an
  advanced-generation refresh, survives dashboard exit and supervisor
  restart, and rejects an unknown prior identity. Assert credential material
  appears only in the dedicated official-daemon response.
- [x] Add one contention scenario in which same-home maintenance hangs and is
  preempted and reaped before the internal callback deadline while observers
  never answer the server request.
- [x] Do not enumerate request malformations, maintenance timing variants, or
  disconnect permutations already covered by the protocol and worker tests.

### Implementation

- [x] Keep one long-lived official daemon connection in the lean supervisor.
  Decode only the small external-refresh request subset through the audited
  `providers/codex/broker/wire.py` leaf. That leaf must not import auth,
  credential, HTTP, maintenance, persistence, or usage modules.
- [x] Validate request ID, previous account ID, selected account ID, daemon
  generation, and current activation state before dispatch.
- [x] Dispatch the foundation's reserved callback worker. It opens only the
  matching managed private home, invokes forced official refresh, proves the
  post-state, and returns one bounded non-persisted auth response.
- [x] Hold the returned token in a redacted dedicated response value only
  until it is serialized to the official daemon. Drop all references
  immediately afterward.
- [x] Never send that response over the Sidekick dashboard socket or worker
  result persistence. The worker-to-supervisor reply uses a dedicated
  owner-only inherited pipe with a one-response limit.
- [x] If a lower-priority operation holds the same home, use the foundation's
  cancellation and reap policy. Never wait behind an unrelated operation.
- [x] Return a typed external-auth error within the internal eight-second
  budget when refresh cannot be proven.
- [x] Commit the verified managed authority before dispatching the daemon
  response. Update selected runtime state only after dispatch and
  scheduler-confirmed worker completion.
- [x] Reconnect, capability-probe, reinstall the last provider-verified
  selection, and re-register exactly one responder after daemon or supervisor
  restart.

### Verify and commit

- [x] Run the two broker scenarios plus the existing foundation protocol and
  worker regressions they depend on.
- [x] Measure callback p95 under a hung general worker and record it separately
  from official daemon latency.
  The 20-sample internal callback result was 1.779 seconds p95, with a
  1.883-second maximum. This does not measure official daemon latency.
- [x] Run Ruff, `ty`, and architecture checks, then commit.

## 10. Task 6 — Codex Activation and Crash Recovery

**Commit:** `feat(codex): activate verified shared accounts`

### Tests first

- [x] Extend `tests/test_codex_managed_runtime.py` with one activation
  scenario that switches A to B through capability preflight, official
  install, correlated-ready proof, commit, and event publication. Prove a
  failed target cannot select another account and Claude state is untouched.
- [x] Add one interruption scenario at the externally meaningful boundary
  after official mutation and before commit. If native auth did not change,
  startup must idempotently reinstall and re-prove the journaled target. A
  deliberate external native login wins. Recovery must serialize concurrent
  retry and never infer an unavailable daemon identity.
- [x] Do not force death after every internal journal write or enumerate all
  equivalent prior-state spellings.

### Implementation

- [x] Add `CodexActivationService` under `credentials/` using the foundation
  activation journal and provider lock.
- [x] Preflight executable, schema, daemon, broker, target authority,
  higher-level service readiness, and expected identity before journal
  creation.
- [x] Force a target private-home read/refresh as due policy requires.
- [x] Journal `prepared`, install external auth, journal
  `target_activated`, require correlated readiness, journal
  `provider_proof_verified`, then atomically commit selected state and
  terminal journal outcome.
- [x] On interruption, compare the read-only native-auth observation with the
  journal baseline first:
  - a deliberate changed saved or external identity wins and reconciles;
  - an unchanged native baseline permits idempotent target reinstall and
    correlated proof;
  - daemon replacement requires reconnect and reinstall; and
  - unreadable or ambiguous state enters reconciliation-required.
- [x] Rehydrate the verified selection after daemon restart.
- [x] Publish only sanitized progress and completion events.

### Verify and commit

- [x] Run the two activation scenarios plus existing journal and daemon
  restart regressions they touch.
- [x] Run Ruff and `ty`, inspect journal fixtures for identities and secrets,
  then commit.

## 11. Task 7 — External Login Reconciliation and Maintenance Integration

**Commit:** `feat(codex): reconcile external account changes`

### Tests first

- [x] Extend the closest existing Codex maintenance test with one
  multi-account scenario: A fails and retains timestamped stale metrics, B
  refreshes and records current metrics, and selection does not change which
  private home is maintained.
- [x] Add one reconciliation scenario to
  `tests/test_codex_managed_runtime.py` where an external official login races
  activation and wins. A known identity is related, an unknown identity
  remains an external state, and neither is silently imported.
- [x] Adapt existing heartbeat, activity, and usage assertions instead of
  duplicating the multi-account scenario in each suite.

### Implementation

- [x] Treat `account/updated` as a change signal, then reconcile a read-only
  native-auth identity and generation against stable provider identities.
- [x] When the identity matches a saved account, update selected state only
  after the external native-auth transition is proven.
- [x] When it is unknown, store non-secret external-active state and block
  automatic attribution or metrics ownership.
- [x] Let a deliberate external login win over a stale Sidekick journal.
- [x] Route scheduled refresh, explicit refresh, heartbeat, usage, and token
  activity through the managed authority coordinator for every saved Codex
  account.
- [x] Keep credential health, metrics freshness, and active state independent.
- [x] Timestamp stale metrics and retain the last exact account-scoped value.
- [x] Enqueue immediate due work on 401, startup, network recovery, migration,
  explicit refresh, runtime restart, and persisted retry.

### Verify and commit

- [x] Run the two reconciliation/maintenance scenarios plus existing usage,
  heartbeat, activity, and queue regressions they touch.
- [x] Run Ruff, `ty`, and architecture checks, then commit.

## 12. Task 8 — Remove Direct OAuth and Copied-Auth Paths

**Commit:** `refactor(codex): remove duplicate token refresh ownership`

### Tests first

- [x] Extend the existing architecture check once to reject private OAuth,
  native `auth.json` copy/write, direct default-home adoption, and token
  serialization outside qualified provider state.
- [x] Replace obsolete direct-OAuth and copied-bundle assertions with one
  command-boundary test proving repair starts independent official login in
  the final private home. Delete superseded tests rather than retaining both
  implementations.

### Implementation

- [x] Remove direct token refresh from `providers/codex/provider.py`.
- [x] Remove copied-bundle creation and native import from
  `credentials/codex/coordinator.py`, `providers/codex/auth_migration.py`,
  and their persistence transaction paths.
- [x] Remove obsolete private OAuth schemas, request helpers, tests, and error
  copy.
- [x] Retain protected managed-home `auth.json` reading only for worker-scoped
  verification. Permit native authentication read-and-immediate-discard only
  for non-secret external-login identity and generation observation.
- [x] Make all unmanaged legacy Codex accounts report migration or login
  required; never silently fall back.
- [x] Search the repository for duplicate token writers, native-home copy
  paths, and private OAuth endpoints. Remove every final-production path.

### Verify and commit

- [x] Run the smallest load-bearing Task 8 checks:

```bash
rg -n \
  "auth\\.openai\\.com/oauth/token|copy.*auth\\.json|auth\\.json.*copy" \
  src tests
uv run pytest -q \
  tests/test_cli_provider_credentials.py::\
test_codex_login_migrates_accounts_independently_without_native_copy \
  tests/test_credential_refresh_codex.py::\
test_managed_codex_maintenance_continues_across_account_failure \
  tests/test_architecture.py::\
test_real_tree_satisfies_every_static_architecture_contract \
  tests/test_architecture.py::\
test_every_static_rule_rejects_a_deliberate_violation \
  tests/test_codex_provider.py::\
test_usage_validates_current_shape_and_required_headers \
  tests/test_credential_service.py::\
test_source_failures_remain_distinct_and_tokens_are_secret_safe
uv run python packaging/check_architecture.py
uv run ruff check src/ tests/ packaging/
uv run ty check src/ tests/
```

- [x] The search may match explicit negative architecture-test strings only.
  It must match no production call or copy path.
- [x] Delete superseded direct-OAuth and copied-auth tests rather than running
  or replacing them. The full-project gate remains Task 9.
- [x] Commit after the focused Task 8 gates are green.

## 13. Task 9 — Codex Phase Gate

This is a verification-only gate. It creates no duplicate end-to-end,
compatibility, restart, unsupported-surface, or secret matrix and requires no
empty commit.

- [x] Map every Codex completion statement below to the smallest task test,
  foundation test, static check, or later authorized live check that proves
  it.
- [x] Confirm the task scenarios collectively cover two independent homes,
  all-account maintenance, A-to-B selection, two observers, broker refresh,
  failure isolation, restart, and pre-mutation capability rejection.
- [x] If a critical completion statement has no evidence, add one focused
  assertion to the nearest existing test. Do not create a Codex phase-gate
  test file. The review found no missing behavioral assertion; unsupported
  session behavior required product documentation only.
- [x] Run the full project gate from the foundation plan.
- [x] Confirm the exact source and installed-wheel entry-point inventories
  contain no `codex` or `claude` command. The package creates no provider
  wrapper, alias, or shell configuration. Retain the real before/after path
  and symlink comparison for the authorized current-machine rollout.
- [x] Confirm no real provider or current-machine mutation occurred.

### Evidence map

- Independent homes, official managed writes, advanced generations,
  no-secret account indexes, native-auth preservation, and no private OAuth:
  `test_codex_login_migrates_accounts_independently_without_native_copy`,
  `test_managed_codex_maintenance_continues_across_account_failure`,
  `test_managed_codex_refresh_fails_closed`, and `CODEX001`.
- Exact preflight and correlated A-to-B activation:
  `test_shared_codex_runtime_rejects_each_preflight_authority` and
  `test_codex_activation_commits_only_correlated_target`.
- Singleton resident refresh after dashboard disconnect, two observers, and
  restart rehydration:
  `test_resident_broker_refreshes_and_recovers_provider_ahead_state` and
  `test_shared_codex_runtime_is_idempotent_and_rehydrates`.
- Hung-worker isolation and cleanup:
  `test_callback_preempts_stubborn_same_home_maintenance`.
- External-login precedence and crash recovery:
  `test_codex_activation_recovers_at_official_mutation_boundary`.
- Every account's independent maintenance, heartbeat, and metrics:
  `test_managed_codex_maintenance_continues_across_account_failure`.
- Supported and unsupported session behavior: the README Codex section and
  this plan's Global Constraints. Package command ownership:
  `test_source_derived_artifact_contract` plus exact installed-wheel smoke.
- Current-machine mutation remains intentionally deferred to the dashboard
  rollout. Every automated scenario uses synthetic identities, fake providers,
  and temporary application roots.

## 14. Codex Completion Gate

Do not begin the Claude plan until all statements are true:

- [x] Every test account has one independently authenticated final private
  home.
- [x] Official managed Codex is the only durable credential writer.
- [x] Forced refresh proves same identity and advanced generation.
- [x] The account index contains no Codex tokens.
- [x] The native default `auth.json` is never copied or written.
- [x] Direct private OAuth refresh no longer exists.
- [x] Exact-version and schema preflight occurs before shared auth mutation.
- [x] The release-gated correlated proof establishes every successful
  activation without claiming daemon account-ID read-back.
- [x] Exactly one broker answers refresh and survives dashboard exit.
- [x] A hung worker cannot consume the callback deadline.
- [x] New and daemon-connected supported TUIs receive account updates.
- [x] Pre-daemon and unsupported launch modes are stated accurately.
- [x] External official login wins without silent import.
- [x] Every unselected account remains maintained and measured.
- [x] Package entry points leave normal `codex` resolution unchanged.
- [x] No live current-machine migration has run.
