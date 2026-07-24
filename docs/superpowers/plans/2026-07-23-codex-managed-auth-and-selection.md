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
socket, Pydantic 2.13.4, Portalocker 3.2.0, pytest 9, Ruff, `ty`, and the
release-matched Codex CLI schema.

## Global Constraints

- Complete
  `2026-07-23-global-account-selection-foundation.md` first.
- The approved design and tracked research are normative. Revalidate the
  installed binary and current release-matched upstream source before
  implementing an unstable method.
- At planning time the inspected executable is
  `/home/sabossedgh/.local/bin/codex`, version `0.145.0`.
- Sidekick never POSTs to Codex's private OAuth endpoint.
- Sidekick never copies, edits, replaces, or derives credentials from the
  native default `~/.codex/auth.json`.
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
- A capability failure disables Codex switching before touching native auth.
  There is no fallback to `auth.json`, direct OAuth, `codex exec`, or copied
  credentials.
- The Sidekick supervisor has one Codex daemon connection and one refresh
  responder. Dashboard and TUI connections never race to answer refresh.
- `codex exec`, native Windows, pre-daemon embedded TUIs, and launch
  configurations that bypass daemon reuse remain accurately unsupported.
- Pre-daemon TUIs require one restart after enrollment. Later supported TUIs
  update on their next safe authenticated request. In-flight requests are
  never retargeted.
- Automated tests use fake binaries, app servers, sockets, clocks, and
  credential homes. They never use a real Codex account or public network.
- Do not add a JSON-RPC dependency. The consumed protocol is narrow, strict,
  bounded, and release-gated.
- Keep provider-specific code in `providers/codex/`, transaction policy in
  `credentials/`, and qualified state in `persistence/`.
- Commit after each numbered task with the listed message. Do not push until
  explicitly authorized.

---

- **Status:** Approved; blocked on foundation implementation
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
- read back the daemon's active identity;
- receive and answer an external refresh request; and
- report unsupported session surfaces.

## 2. Target File Map

Create focused modules rather than expanding the current 615-line `auth.py`,
536-line `credentials/codex.py`, or 427-line `auth_migration.py`:

- `providers/codex/executable.py`: exact executable and version discovery;
- `providers/codex/capabilities.py`: generated-schema and version gate;
- `providers/codex/jsonrpc.py`: bounded JSON-lines framing and correlation;
- `providers/codex/app_server.py`: private managed app-server lifecycle;
- `providers/codex/account.py`: strict account read and generation snapshots;
- `providers/codex/login.py`: final-home official login workflow;
- `providers/codex/daemon.py`: official daemon lifecycle and socket discovery;
- `providers/codex/broker_wire.py`: lightweight bounded daemon messages;
- `providers/codex/external_auth.py`: strict unstable external-auth messages;
- `providers/codex/broker.py`: provider-specific callback validation;
- `providers/codex/session_support.py`: supported/unsupported launch status;
- `credentials/codex_authorities.py`: managed-home orchestration;
- `credentials/codex_activation.py`: provider activation transaction;
- `credentials/codex_reconciliation.py`: native external-choice policy; and
- `persistence/codex_authorities.py`: no-secret managed metadata transaction.

Keep `providers/codex/auth.py` as the protected credential-envelope reader and
official auth subprocess boundary. Remove obsolete responsibilities after
callers move; leave no second implementation.

## 3. Task 1 — Exact Executable, Schema, and JSON-RPC Boundary

**Commit:** `feat(codex): add versioned app-server boundary`

### Tests first

- [ ] Add `tests/test_codex_executable.py` for missing executable, ambiguous
  path, wrong vendor output, unsupported version, changed executable during an
  operation, and exact absolute argv.
- [ ] Add `tests/test_codex_capabilities.py` with synthetic generated schemas
  for:
  - all required managed account methods;
  - missing `account/read`;
  - missing forced-refresh parameter;
  - missing external token login;
  - missing external refresh request;
  - incompatible field shapes;
  - a CLI/daemon version mismatch; and
  - unknown additive fields that remain outside Sidekick's strict consumed
    subset.
- [ ] Add `tests/test_codex_jsonrpc.py` for initialize ordering, ID
  correlation, notifications, server requests, fragmented lines, duplicate
  keys, invalid UTF-8, oversized input, unexpected responses, timeout, child
  exit, and redacted stderr.
- [ ] Add a fixture generator that invokes only a synthetic Codex executable.
  Automated tests must not invoke the installed binary.
- [ ] Run the focused tests and confirm failure because the versioned
  app-server boundary does not exist.

### Implementation

- [ ] Resolve Codex once through `shutil.which`, require an absolute regular
  executable, obtain `codex --version`, and retain immutable provenance for
  the operation.
- [ ] Generate the experimental JSON schema in a qualified temporary
  directory using:

```bash
SIDEKICK_CODEX_SCHEMA_DIR=$(mktemp -d)
codex app-server generate-json-schema \
  --experimental \
  --out "$SIDEKICK_CODEX_SCHEMA_DIR"
```

- [ ] Parse only the generated files required for managed `account/read`,
  official login, `chatgptAuthTokens`, `account/updated`, and
  `account/chatgptAuthTokens/refresh`.
- [ ] Hash the capability schema only for local compatibility-cache
  invalidation. Never log or confuse that schema hash with a credential
  generation.
- [ ] Pin compatibility to the exact major/minor/patch and required schema
  shapes. A new installed version is unsupported until the probe passes.
- [ ] Implement a bounded JSON-RPC session over stdio with:
  - 1 MiB maximum line;
  - strict duplicate-key rejection;
  - monotonically allocated request IDs;
  - explicit initialize then initialized ordering;
  - separate response, notification, and server-request types;
  - monotonic deadlines; and
  - forced child termination and reaping on failure.
- [ ] Use a minimal child environment with the requested private
  `CODEX_HOME`. Do not inherit credential environment variables.
- [ ] Redact provider output at the subprocess boundary before raising typed
  errors.
- [ ] Update architecture checks so generic JSON-RPC framing does not import
  credentials, HTTP, CLI, or presentation modules.

### Verify and commit

- [ ] Run:

```bash
uv run pytest \
  tests/test_codex_executable.py \
  tests/test_codex_capabilities.py \
  tests/test_codex_jsonrpc.py \
  tests/test_codex_provider.py \
  tests/test_architecture.py
```

- [ ] Run Ruff and `ty`, inspect every subprocess call for absolute argv and
  bounded output, then commit.

## 4. Task 2 — Managed Private-Home Read and Refresh

**Commit:** `feat(codex): refresh managed private authorities`

### Tests first

- [ ] Add `tests/test_codex_app_server.py` for initialized session lifecycle,
  clean close, timeout, malformed messages, child failure, and concurrent
  private homes.
- [ ] Add `tests/test_codex_managed_account.py` for:
  - `account/read` with `refreshToken: false`;
  - `account/read` with `refreshToken: true`;
  - same-account advanced generation;
  - same-account unchanged generation;
  - regressed generation;
  - null account;
  - wrong account;
  - malformed protected state;
  - provider success with unchanged protected state;
  - transient failure with unchanged authority;
  - rejected credential;
  - file-backed storage requirement; and
  - exact private-home containment.
- [ ] Add serialization tests proving access, refresh, and ID tokens never
  enter the managed account index or safe outcomes.
- [ ] Add lock tests for same-home serialization and different-home
  independence.

### Implementation

- [ ] Derive each private `CODEX_HOME` from the stable account ID through
  `paths.py`. Never accept a user-supplied home for a managed authority.
- [ ] Require official file-backed Codex auth storage in private homes.
- [ ] Snapshot the protected auth envelope before and after the app-server
  operation using the existing strict reader. Keep token values
  non-represented and inside the worker.
- [ ] Implement read-only state with `account/read` and
  `refreshToken: false`.
- [ ] Implement forced refresh with:

```json
{
  "method": "account/read",
  "params": {
    "refreshToken": true
  }
}
```

- [ ] Treat the app-server response as necessary but insufficient. Success
  requires:
  - a non-null account;
  - expected stable provider identity;
  - valid protected post-state;
  - the same identity before and after; and
  - an advanced provider-owned generation for forced refresh.
- [ ] Return strict sanitized outcomes for healthy, unchanged, rejected,
  logged out, incompatible, malformed, timeout, and transient states.
- [ ] Persist only generation, identity, version, timestamps, health, and
  action-required metadata.
- [ ] Use the qualified authority lock shared by scheduled refresh, broker
  refresh, migration, and activation.

### Verify and commit

- [ ] Run managed account, existing Codex provider, refresh-authority,
  credential-output, and locking tests.
- [ ] Run Ruff, `ty`, and architecture checks.
- [ ] Confirm no direct network request exists in the new managed path, then
  commit.

## 5. Task 3 — Independent Final-Home Login and Legacy Migration

**Commit:** `feat(codex): migrate accounts to independent managed homes`

### Tests first

- [ ] Add `tests/test_codex_managed_login.py` for app-server login start,
  browser URL presentation, completion notification, cancellation, timeout,
  wrong identity, revoked old auth, and final-home persistence.
- [ ] Extend `tests/test_codex_auth_migration.py` for:
  - no native `auth.json` import;
  - no private-home copy;
  - one independently authenticated home per account;
  - legacy authority retained on cancel or mismatch;
  - managed metadata committed before legacy retirement;
  - preserved label, plan, heartbeat, and metrics history; and
  - one failed account not blocking the next.
- [ ] Add a recovery test that kills Sidekick after official login succeeds
  but before the account index commits. Recovery must inspect the private home
  and either finish the same-identity commit or require reconciliation.
- [ ] Add a test proving two labels cannot adopt one private home or provider
  identity accidentally.

### Implementation

- [ ] Allocate the final stable home before login and authenticate it in place.
  Do not use a disposable source directory.
- [ ] Prefer app-server `account/login/start` and its completion
  notifications. Use a narrowly controlled `codex login` child only if the
  exact supported binary lacks the required managed login method and the
  approved design is formally updated first.
- [ ] Surface the provider URL or device step through a sanitized worker event.
  Never put provider credentials in the event.
- [ ] Allow only unavoidable user browser, MFA, password, or consent input.
- [ ] Verify the final provider identity against the saved logical account
  before converting authority metadata.
- [ ] Record the managed authority transaction, prove official refresh, then
  retire the legacy duplicated credential. Preserve it on every unsuccessful
  path.
- [ ] Migrate accounts independently and continue after account-scoped manual
  action.
- [ ] Update `codex-login` and repair command workflows to use final managed
  homes. Remove any option that treats the active native login as a source for
  managed accounts.
- [ ] Keep the default native home unchanged throughout migration.

### Verify and commit

- [ ] Run managed-login, auth-migration, persistence transaction, recovery,
  CLI Codex credential, and output-safety suites.
- [ ] Run Ruff and `ty`.
- [ ] Inspect fake subprocess events and staged artifacts for token
  duplication, then commit.

## 6. Task 4 — Official Shared-Daemon Lifecycle and Read-Back

**Commit:** `feat(codex): manage official shared daemon`

### Tests first

- [ ] Add `tests/test_codex_shared_daemon.py` for:
  - `codex app-server daemon start`;
  - idempotent start;
  - version JSON validation;
  - socket readiness;
  - daemon restart;
  - missing socket;
  - stale socket;
  - local/daemon version mismatch;
  - unexpected socket ownership;
  - daemon exit and reconnect; and
  - no write to default `auth.json`.
- [ ] Add `tests/test_codex_external_auth.py` for exact
  `chatgptAuthTokens` install, bounded token inputs, `account/updated`,
  expected identity read-back, wrong identity, null identity, malformed
  response, and missing capability.
- [ ] Add two fake TUI clients and prove both receive one account update from
  the daemon.
- [ ] Add a preflight test proving incompatibility fails before external
  account installation.

### Implementation

- [ ] Manage the official daemon with the resolved executable:

```bash
codex app-server daemon start
codex app-server daemon version
```

- [ ] Connect through the official default Unix control socket only after
  verifying owner, type, version, and readiness.
- [ ] Keep daemon lifecycle distinct from the Sidekick supervisor lifecycle.
  Sidekick may start and reconnect to it but does not replace its executable or
  socket.
- [ ] Perform a fresh generated-schema capability probe before first external
  auth installation after either process version changes.
- [ ] Obtain a fresh selected-account lease inside a high-priority worker and
  construct the exact release-matched `chatgptAuthTokens` request.
- [ ] Send the ephemeral access token and account ID only to the official
  daemon connection. Do not write either to default `auth.json`.
- [ ] Require `account/updated` plus explicit active-account read-back matching
  the target identity before returning activation success.
- [ ] Mark the provider not ready on daemon restart until the selected
  projection is rehydrated and verified.
- [ ] Detect daemon-connected support separately from `codex exec`, embedded
  pre-daemon TUIs, native Windows, and launch modes that bypass daemon reuse.

### Verify and commit

- [ ] Run all shared-daemon and external-auth tests.
- [ ] Run the daemon priority and import audits from the foundation.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 7. Task 5 — Singleton Refresh Broker

**Commit:** `feat(codex): answer shared-daemon refresh requests`

### Tests first

- [ ] Add `tests/test_codex_broker.py` for:
  - exactly one responder registration;
  - request routing by previous Codex account ID;
  - selected-account match;
  - stale or unknown identity rejection;
  - forced managed refresh;
  - same-account advanced generation;
  - unchanged or regressed generation failure;
  - malformed request;
  - duplicate request ID;
  - daemon disconnect;
  - supervisor restart and rehydration;
  - response before the ten-second provider deadline; and
  - secret absence outside the dedicated daemon response.
- [ ] Add contention tests where same-home maintenance is healthy, completed,
  hung, or killed.
- [ ] Add a broadcast test proving fake TUI clients observe but never answer
  the server request.
- [ ] Add a dashboard-exit test proving the resident broker stays connected.

### Implementation

- [ ] Keep one long-lived official daemon connection in the lean supervisor.
  Decode only the small external-refresh request subset through the audited
  `providers.codex.broker_wire` leaf. That leaf must not import app-server
  process, auth, credential, HTTP, maintenance, or usage modules.
- [ ] Validate request ID, previous account ID, selected account ID, daemon
  generation, and current activation state before dispatch.
- [ ] Dispatch the foundation's reserved callback worker. It opens only the
  matching managed private home, invokes forced official refresh, proves the
  post-state, and returns one bounded non-persisted auth response.
- [ ] Hold the returned token in a redacted dedicated response value only
  until it is serialized to the official daemon. Drop all references
  immediately afterward.
- [ ] Never send that response over the Sidekick dashboard socket or worker
  result persistence. The worker-to-supervisor reply uses a dedicated
  owner-only inherited pipe with a one-response limit.
- [ ] If a lower-priority operation holds the same home, use the foundation's
  cancellation and reap policy. Never wait behind an unrelated operation.
- [ ] Return a typed external-auth error within the internal eight-second
  budget when refresh cannot be proven.
- [ ] Update managed authority generation and health transactionally after the
  daemon response is accepted.
- [ ] Reconnect, capability-probe, reinstall the last provider-verified
  selection, and re-register exactly one responder after daemon or supervisor
  restart.

### Verify and commit

- [ ] Run broker, priority, managed refresh, shared daemon, protocol,
  supervisor restart, and secret-leak suites.
- [ ] Measure callback p95 under a hung general worker and record it separately
  from official daemon latency.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 8. Task 6 — Codex Activation and Crash Recovery

**Commit:** `feat(codex): activate verified shared accounts`

### Tests first

- [ ] Add `tests/test_codex_activation.py` for every activation phase:
  capability preflight, fresh target, external install, account update,
  read-back, commit, and event publication.
- [ ] Force process death after each journal phase and prove startup recovery
  follows actual daemon identity.
- [ ] Cover target already active, prior account active, unrelated saved
  account active, unknown external account active, logged out, malformed
  state, and daemon unavailable.
- [ ] Prove a failed target does not silently activate another account.
- [ ] Prove Claude selected state is not touched by a Codex switch.
- [ ] Prove two simultaneous Codex activations serialize and the second uses
  refreshed read-back rather than a stale source.

### Implementation

- [ ] Add `CodexActivationService` under `credentials/` using the foundation
  activation journal and provider lock.
- [ ] Preflight executable, schema, daemon, broker, target authority,
  higher-level service readiness, and expected identity before journal
  creation.
- [ ] Force a target private-home read/refresh as due policy requires.
- [ ] Journal `prepared`, install external auth, journal
  `target_activated`, verify daemon identity, journal
  `read_back_verified`, then atomically commit selected state and terminal
  journal outcome.
- [ ] On interruption, read the daemon first:
  - matching target completes commit;
  - matching source records rollback;
  - another deliberate saved or external identity wins and reconciles;
  - logged-out state remains logged out; and
  - unreadable state enters reconciliation-required.
- [ ] Rehydrate the verified selection after daemon restart.
- [ ] Publish only sanitized progress and completion events.

### Verify and commit

- [ ] Run activation, journal, selected-state, concurrency, recovery, and
  daemon restart suites.
- [ ] Run Ruff and `ty`, inspect journal fixtures for identities and secrets,
  then commit.

## 9. Task 7 — External Login Reconciliation and Maintenance Integration

**Commit:** `feat(codex): reconcile external account changes`

### Tests first

- [ ] Add `tests/test_codex_reconciliation.py` for:
  - external switch to another saved account;
  - external login to an unknown identity;
  - external logout;
  - external login racing Sidekick activation;
  - malformed external state;
  - no silent import;
  - temporary external dashboard state; and
  - explicit later import workflow.
- [ ] Update `tests/test_credential_refresh_codex.py`,
  `tests/test_codex_heartbeat.py`, `tests/test_codex_activity.py`, and
  `tests/test_usage_service.py` so every account uses its managed private home
  regardless of selected state.
- [ ] Add a multi-account test where account A is rejected, B refreshes, A
  still shows stale metrics, and B records current metrics.

### Implementation

- [ ] Reconcile `account/updated` and explicit startup read-back against stable
  provider identities.
- [ ] When the identity matches a saved account, update selected state only
  after read-back proof.
- [ ] When it is unknown, store non-secret external-active state and block
  automatic attribution or metrics ownership.
- [ ] Let a deliberate external login win over a stale Sidekick journal.
- [ ] Route scheduled refresh, explicit refresh, heartbeat, usage, and token
  activity through the managed authority coordinator for every saved Codex
  account.
- [ ] Keep credential health, metrics freshness, and active state independent.
- [ ] Timestamp stale metrics and retain the last exact account-scoped value.
- [ ] Enqueue immediate due work on 401, startup, network recovery, migration,
  explicit refresh, runtime restart, and persisted retry.

### Verify and commit

- [ ] Run reconciliation, maintenance, usage, heartbeat, activity, queue, and
  multi-account suites.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 10. Task 8 — Remove Direct OAuth and Copied-Auth Paths

**Commit:** `refactor(codex): remove duplicate token refresh ownership`

### Tests first

- [ ] Add an architecture check rejecting Codex private OAuth host/path
  constants and credential-bearing refresh HTTP requests.
- [ ] Add source-contract tests rejecting native `auth.json` copy, direct
  default-home adoption, and token serialization in account metadata.
- [ ] Update old tests so they assert managed app-server behavior instead of
  direct OAuth responses or copied bundles.
- [ ] Add command tests proving repair starts independent official login and
  never asks the user to make the desired account native first.

### Implementation

- [ ] Remove direct token refresh from `providers/codex/provider.py`.
- [ ] Remove copied-bundle creation and native import from
  `credentials/codex.py`, `providers/codex/auth_migration.py`, and their
  persistence transaction paths.
- [ ] Remove obsolete private OAuth schemas, request helpers, tests, and error
  copy.
- [ ] Retain protected `auth.json` reading only for worker-scoped verification
  of the exact private managed home.
- [ ] Make all unmanaged legacy Codex accounts report migration or login
  required; never silently fall back.
- [ ] Search the repository for duplicate token writers, native-home copy
  paths, and private OAuth endpoints. Remove every final-production path.

### Verify and commit

- [ ] Run:

```bash
rg -n \
  "auth\\.openai\\.com/oauth/token|copy.*auth\\.json|auth\\.json.*copy" \
  src tests
uv run pytest tests/test_codex_*.py tests/test_credential_refresh_codex.py
uv run python packaging/check_architecture.py
uv run ruff check src/ tests/
uv run ty check src/ tests/
```

- [ ] The search may match explicit negative architecture-test strings only.
  It must match no production call or copy path.
- [ ] Commit after the complete Codex suite is green.

## 11. Task 9 — Codex Phase Gate

**Commit:** `test(codex): verify managed multi-account selection`

- [ ] Add one end-to-end fake scenario with two independently authenticated
  homes, all-account maintenance, shared-daemon selection A to B, two
  connected TUIs, a broker refresh for B, and continued maintenance for A.
- [ ] Add one compatibility scenario proving missing internal capability
  blocks before native mutation and preserves the prior selected account.
- [ ] Add one restart scenario proving daemon and Sidekick restart rehydrate
  the last provider-verified account and exactly one broker.
- [ ] Add one unsupported-surface scenario for `codex exec`, pre-daemon TUI,
  daemon-bypassing configuration, and native Windows.
- [ ] Add one complete secret matrix covering private worker errors, JSON-RPC,
  daemon messages, broker pipes, Sidekick protocol, persistence, logs,
  arguments, and representations.
- [ ] Run the full project gate from the foundation plan.
- [ ] Confirm ordinary `codex` path and symlink resolution remain unchanged in
  a synthetic installation test.
- [ ] Confirm no real provider or current-machine mutation occurred.
- [ ] Commit the phase evidence.

## 12. Codex Completion Gate

Do not begin the Claude plan until all statements are true:

- [ ] Every test account has one independently authenticated final private
  home.
- [ ] Official managed Codex is the only durable credential writer.
- [ ] Forced refresh proves same identity and advanced generation.
- [ ] The account index contains no Codex tokens.
- [ ] The native default `auth.json` is never copied or written.
- [ ] Direct private OAuth refresh no longer exists.
- [ ] Exact-version and schema preflight occurs before shared auth mutation.
- [ ] The official daemon read-back proves every successful activation.
- [ ] Exactly one broker answers refresh and survives dashboard exit.
- [ ] A hung worker cannot consume the callback deadline.
- [ ] New and daemon-connected supported TUIs receive account updates.
- [ ] Pre-daemon and unsupported launch modes are stated accurately.
- [ ] External official login wins without silent import.
- [ ] Every unselected account remains maintained and measured.
- [ ] Normal `codex` resolution is unchanged.
- [ ] No live current-machine migration has run.
