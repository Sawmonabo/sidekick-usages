# Claude Managed Authentication and Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every saved Claude account healthy in a stable private profile,
preserve fixed-lifetime setup tokens, and switch the native subscription login
through official Claude processes so normal and supported ongoing `claude`
sessions use the provider-verified selected account.

**Architecture:** Allocate one immutable `CLAUDE_CONFIG_DIR` per logical
account. Linux and WSL use that profile's protected credential file; macOS
uses Claude's config-derived Keychain service. Workers may read protected
credential state, but only `claude auth login` writes native or private
subscription credentials. Activation first retains the outgoing native
generation in its private profile, officially provisions the target into the
native authority, and commits only after identity and generation read-back.

**Tech Stack:** Python 3.14, the foundation authority and activation
transactions, official Claude Code CLI, Linux/WSL protected credential files,
macOS Keychain through `/usr/bin/security`, Pydantic 2.13.4, Portalocker
3.2.0, pytest 9, Ruff, and `ty`.

## Global Constraints

- Complete the foundation and Codex plans first.
- The approved design and tracked research are normative. Revalidate the
  installed Claude binary before implementation and before each live platform
  rollout.
- At planning time the inspected Claude executable is version `2.1.218`.
- Normal `claude` remains Anthropic's installed executable. Sidekick creates
  no wrapper, alias, shell function, PATH shim, symlink replacement, or shell
  startup edit.
- Only official `claude auth login` may durably write subscription
  credentials to a native or private profile.
- Sidekick never manually edits a Claude credential file or Keychain item.
- Sidekick may read a qualified protected credential envelope inside a
  bounded worker for verification and official credential handoff.
- A refresh token required by official login is passed only through a minimal
  closed child environment. It is never placed in argv, a general inherited
  environment, persistence, logs, exceptions, or the Sidekick socket.
- Each selectable subscription account has one stable absolute private
  `CLAUDE_CONFIG_DIR` derived from its Sidekick account ID.
- The selected account's native authority and every inactive private
  authority are distinct, explicitly verified locations.
- A switch is successful only after provider identity read-back. A failed
  switch never guesses from a Sidekick pointer.
- Claude setup tokens remain separate fixed-lifetime credentials. Sidekick
  tracks and measures them but never calls their maintenance `refresh`.
- Enter on a setup-token-only account starts official subscription-login
  migration. It never pretends the setup token can become bare `claude`
  native auth.
- After migration, one logical row may retain both a managed subscription
  authority and its setup token. Usage and activity are attributed once.
- Higher-priority API key, auth token, gateway, cloud-provider, or helper
  credentials stay outside subscription selection. Sidekick reports the
  conflict and never clears the user's environment.
- On macOS, a locked/unavailable Keychain or plaintext fallback fails closed.
  Sidekick never requests or stores the macOS password.
- Remote Control disruption is the only planned routine extra confirmation.
  Healthy ordinary switches require one Enter press from the dashboard.
- Native Windows is feature-disabled for this first release. Linux, WSL,
  macOS arm64, and macOS x64 are required.
- Automated tests use synthetic profiles, fake Keychains, fake binaries,
  fake clocks, and fake provider identities. They never mutate real Claude
  state or use public network access.
- Commit after each numbered task with the listed Conventional Commit
  message. Do not push until explicitly authorized.

---

- **Status:** Approved; blocked on foundation and Codex implementation
- **Date:** 2026-07-23
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Planning baseline:** `dfde7d8c3b1855e2307ed2fc24fb8a72497ed39d`
- **Installed Claude baseline:** `2.1.218`
- **Required platforms:** Linux, WSL, macOS arm64, macOS x64
- **Previous phase:** `2026-07-23-codex-managed-auth-and-selection.md`
- **Next phase:** `2026-07-23-interactive-account-dashboard-and-rollout.md`

## 1. Final Claude Contract

```text
inactive account A -> stable private profile A
inactive account B -> stable private profile B
                              |
                official Claude transition
                              |
                  native default authority
                              |
               ordinary Claude Code sessions
```

For each logical Claude account, the no-secret account index may reference:

- no subscription authority yet;
- a legacy token-owning subscription authority awaiting migration;
- one managed private subscription authority; and
- an independently preserved setup-token authority.

The provider adapter exposes these typed operations:

- discover and prove the exact Claude executable;
- classify supported platform and installed capabilities;
- derive and validate one stable private profile;
- derive the expected native or config-specific Keychain namespace;
- read strict protected auth status from native or private authority;
- detect higher-priority credential conflicts;
- run official login in a target profile with browser or refresh-token input;
- prove same identity and an acceptable generation transition;
- classify Remote Control disruption; and
- report setup-token fixed-lifetime health.

## 2. Target File Map

Create focused modules rather than extending the current 621-line
`providers/claude/provider.py`, 287-line credentials reader, or 790-line
credential service:

- `providers/claude/executable.py`: exact binary and version discovery;
- `providers/claude/capabilities.py`: platform and auth capability gate;
- `providers/claude/profiles.py`: stable private and native profile identity;
- `providers/claude/keychain.py`: macOS namespace and read-only adapter;
- `providers/claude/auth_status.py`: strict JSON status plus envelope proof;
- `providers/claude/login.py`: official login subprocess boundary;
- `providers/claude/environment.py`: higher-priority credential detection and
  minimal child environment;
- `providers/claude/remote_control.py`: disruption observation;
- `credentials/claude_authorities.py`: private/native maintenance policy;
- `credentials/claude_migration.py`: setup-token and legacy migration;
- `credentials/claude_activation.py`: provider activation transaction;
- `credentials/claude_reconciliation.py`: official external-login policy; and
- `persistence/claude_authorities.py`: no-secret managed metadata transaction.

Refactor `providers/claude/credentials.py` into a narrow protected-envelope
reader. Remove platform and transition responsibilities after callers move.

## 3. Task 1 — Exact Binary, Stable Profiles, and Capability Gate

**Commit:** `feat(claude): add stable profile capability boundary`

### Tests first

- [ ] Add `tests/test_claude_executable.py` for missing, ambiguous, wrong
  vendor, unsupported version, executable replacement, and exact absolute
  argv.
- [ ] Add `tests/test_claude_profiles.py` for:
  - stable profile derivation from account ID;
  - rename preserving the profile;
  - two accounts receiving distinct profiles;
  - absolute canonical paths;
  - containment under the Sidekick private Claude root;
  - native profile distinction; and
  - rejection of symlink/path escape.
- [ ] Add `tests/test_claude_capabilities.py` for Linux, WSL, macOS arm64,
  macOS x64, native Windows disablement, missing auth status, missing auth
  login, missing JSON output, and refresh-token provisioning support.
- [ ] Add a preflight test proving capability failure occurs before a login
  child starts or native state changes.

### Implementation

- [ ] Resolve Claude once through `shutil.which`, require an absolute regular
  executable, capture `claude --version`, and retain immutable provenance for
  the operation.
- [ ] Probe only documented command surfaces:

```bash
claude auth status --json
claude auth login --help
```

- [ ] Combine command probes with version-pinned installed-binary observations
  required for config-specific storage. Keep the latter explicitly marked
  compatibility-sensitive.
- [ ] Derive private profiles only in `paths.py` from stable Sidekick account
  IDs. A label or provider email never appears in the path.
- [ ] Create profile directories with owner-only traversal permissions and
  validate every component before use.
- [ ] Return a closed capability result identifying file-backed Linux/WSL,
  Keychain-backed macOS, and unsupported native Windows.
- [ ] Disable switching before mutation when required auth, storage, identity,
  or official refresh-token provisioning capability is absent.

### Verify and commit

- [ ] Run:

```bash
uv run pytest \
  tests/test_claude_executable.py \
  tests/test_claude_profiles.py \
  tests/test_claude_capabilities.py \
  tests/test_paths.py \
  tests/test_architecture.py
```

- [ ] Run Ruff and `ty`, inspect path derivation and subprocess argv, then
  commit.

## 4. Task 2 — Linux, WSL, and macOS Protected Storage Read-Back

**Commit:** `feat(claude): verify private credential authorities`

### Tests first

- [ ] Extend `tests/test_claude_provider_boundaries.py` for one exact
  `CLAUDE_CONFIG_DIR` rather than default-home-only detection.
- [ ] Add `tests/test_claude_keychain.py` for:
  - native unsuffixed service;
  - two distinct config-derived services;
  - the release-matched
    `Claude Code-credentials-<first 8 SHA-256(config-dir)>` namespace;
  - Apple Silicon and Intel behavior;
  - item missing;
  - Keychain locked;
  - access denied;
  - malformed output;
  - bounded output;
  - command timeout; and
  - no password prompt or write command.
- [ ] Add Linux and WSL cases for expected protected file, wrong permissions,
  symlink, missing file, malformed envelope, oversized envelope, and identity
  mismatch.
- [ ] Add macOS plaintext-fallback detection and prove it blocks both
  maintenance and activation.
- [ ] Add secret-output tests for `/usr/bin/security` stdout and errors.

### Implementation

- [ ] Make credential discovery require an explicit `ClaudeProfile` value.
  Remove the current ignored `credential_home` behavior.
- [ ] On Linux and WSL, read only the protected credential path inside the
  exact profile. Do not search fallback home directories for a managed
  account.
- [ ] On macOS, derive the service name using the exact release-matched path
  rule: SHA-256 of the absolute config-directory string encoded as UTF-8,
  first eight lowercase hexadecimal characters, appended to
  `Claude Code-credentials-`. Use
  `/usr/bin/security find-generic-password` read-only inside a worker.
- [ ] Keep Keychain output bounded, non-represented, and parsed by the strict
  Claude envelope schema.
- [ ] Distinguish missing, malformed, unreadable, locked, access denied,
  plaintext fallback, expired access, expired login, and identity mismatch.
- [ ] Record only provider identity, generation, expiry metadata, health, and
  sanitized action in the account index.
- [ ] Add a compatibility revalidation trigger for any Claude version whose
  profile namespace differs from the pinned observation.

### Verify and commit

- [ ] Run Claude credentials, Keychain, provider-boundary, output-safety,
  filesystem, and architecture tests.
- [ ] Run Ruff and `ty`.
- [ ] Search production code for Keychain mutation commands. Only read-only
  lookup is allowed, then commit.

## 5. Task 3 — Official Private-Profile Maintenance

**Commit:** `feat(claude): maintain accounts through official login`

### Tests first

- [ ] Add `tests/test_claude_official_login.py` for:
  - browser login in a final private profile;
  - refresh-token-provisioned login;
  - exact minimal environment;
  - refresh token absent from argv;
  - scope propagation;
  - status read-back;
  - same identity;
  - wrong identity;
  - unchanged generation;
  - canceled login;
  - timeout;
  - child crash; and
  - redacted provider output.
- [ ] Extend `tests/test_claude_refresh.py` for Linux, WSL, and both macOS
  architectures using only the official CLI write path.
- [ ] Add tests proving scheduled maintenance never targets the native profile
  of an inactive account and never activates an account globally.
- [ ] Add two-account tests proving independent private maintenance and
  continuation after one account failure.

### Implementation

- [ ] Introduce an official-login adapter that runs the exact Claude
  executable with the target `CLAUDE_CONFIG_DIR`.
- [ ] For an existing subscription authority, open its protected refresh
  credential only inside the worker and launch official login with a minimal
  environment containing:
  - `CLAUDE_CONFIG_DIR`;
  - `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`; and
  - `CLAUDE_CODE_OAUTH_SCOPES`, encoded as the validated scopes joined by one
    ASCII space.
- [ ] Do not inherit `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, provider gateways, or cloud credentials into the
  child.
- [ ] Read target status after official login and require expected identity,
  valid protected storage, supported scopes, and an acceptable new
  generation.
- [ ] On macOS, require the expected Keychain item and reject a plaintext
  fallback.
- [ ] Persist only sanitized managed-authority metadata.
- [ ] Serialize maintenance with activation and broker work through the
  qualified authority lock.
- [ ] Classify setup-token authority as fixed-lifetime/not-refreshable. Its
  maintenance path performs health, usage, and lifetime checks only.
- [ ] Remove macOS refresh exclusion once the official profile-specific CLI
  path is proven by tests.

### Verify and commit

- [ ] Run official-login, Claude refresh, maintenance, queue, credential
  authority, and multi-account tests.
- [ ] Run Ruff and `ty`.
- [ ] Inspect child environments through synthetic fakes and confirm no
  parent credential variable leaks, then commit.

## 6. Task 4 — Setup-Token and Legacy Subscription Migration

**Commit:** `feat(claude): migrate accounts to managed profiles`

### Tests first

- [ ] Add `tests/test_claude_managed_migration.py` for:
  - setup-token-only account;
  - legacy subscription-only account;
  - one account with both credential modes;
  - final private profile login;
  - provider identity match;
  - identity mismatch;
  - cancellation;
  - Keychain lock;
  - preserved setup-token secret and expiry;
  - retired legacy subscription secret only after commit;
  - preserved metrics and heartbeat history; and
  - no duplicate account row or token activity.
- [ ] Add crash recovery after official login but before metadata commit.
- [ ] Add a one-account failure test proving later Claude accounts continue.
- [ ] Update setup-token save and restore tests so a preserved setup token is
  one authority on a logical account, not a replacement for its managed
  subscription authority.

### Implementation

- [ ] On Enter or explicit repair for a setup-token-only account, allocate the
  final stable private profile and start official subscription login there.
- [ ] Require user involvement only for provider-controlled browser, MFA,
  password, or consent.
- [ ] Verify the returned stable account and organization identity against the
  saved logical account. A mismatch leaves both the setup token and native
  selection unchanged.
- [ ] Commit managed subscription metadata while retaining the setup-token
  authority and its fixed-lifetime tracking.
- [ ] For a legacy subscription login, use its protected migration authority
  only as input to official login in the final private profile. Retire the
  legacy token store only after managed read-back and refresh proof.
- [ ] Attribute usage, heartbeat, and activity to the logical stable account
  ID so two credential modes do not double-count.
- [ ] Update `setup-token`, restore, remove, reset, rename, and doctor
  workflows for dual authority.
- [ ] Never make migration itself change the native selected account.

### Verify and commit

- [ ] Run managed migration, setup-token, restore, lifetime, account
  transaction, activity, and output-safety tests.
- [ ] Run Ruff, `ty`, and architecture checks.
- [ ] Inspect test account counts and metrics aggregation for duplication,
  then commit.

## 7. Task 5 — Native Activation Transaction

**Commit:** `feat(claude): activate verified native accounts`

### Required transaction

For source S and target T:

1. preflight exact binary, platform, storage, higher-priority credentials,
   target managed authority, Remote Control, locks, and service readiness;
2. read and verify the actual native identity;
3. reconcile that identity to S or an external state;
4. journal `prepared`;
5. use official Claude to provision the latest native S generation into
   private profile S;
6. verify private S identity and generation;
7. journal `outgoing_retained`;
8. use official Claude to provision private T into the native profile;
9. verify native T identity and generation;
10. verify private T remains a usable same-account authority;
11. journal `target_activated` and `read_back_verified`; and
12. atomically commit selected state and terminal journal outcome.

If the exact Claude release invalidates the source private authority during
official target provisioning, stop before release and update the approved
design with evidence. Never ship a switch that leaves the selected account
without a maintainable private authority.

### Tests first

- [ ] Add `tests/test_claude_activation.py` for the complete transaction and
  each preflight failure.
- [ ] Force process death after each numbered mutation or journal boundary.
- [ ] Cover source equals target, target not maintained, source unknown,
  native logged out, wrong target identity, target private authority becoming
  unusable, Keychain lock, and official child timeout.
- [ ] Prove a healthy switch requires one activation request and no extra
  confirmation when Remote Control is inactive.
- [ ] Prove Codex selected state is untouched.
- [ ] Add concurrent activation tests for provider lock and stable account
  lock order.

### Implementation

- [ ] Add `ClaudeActivationService` under `credentials/` and compose only
  provider ports plus foundation persistence transactions.
- [ ] Read actual native state before journaling. Never trust selected state
  as current proof.
- [ ] Retain outgoing credentials only by running official Claude against the
  outgoing stable private profile with a closed refresh-token environment.
- [ ] Activate the target only by running official Claude against the native
  default profile with a closed refresh-token environment from target
  authority.
- [ ] Prove source private, target native, and target private states using
  strict protected read-back.
- [ ] Publish only sanitized progress events. Credential values never leave
  the worker or enter the activation journal.
- [ ] Commit selected state only after target native identity is proven.
- [ ] Keep metrics and maintenance state independent of activation outcome.

### Verify and commit

- [ ] Run activation, journal, selected-state, provider lock, Keychain, and
  secret-leak suites.
- [ ] Run Ruff and `ty`.
- [ ] Review every native write path and confirm it launches official Claude,
  then commit.

## 8. Task 6 — Official Rollback and Reconciliation

**Commit:** `feat(claude): recover interrupted account switches`

### Tests first

- [ ] Add `tests/test_claude_recovery.py` for:
  - no native mutation;
  - target already active;
  - source active;
  - another saved account active;
  - unknown external identity active;
  - logged out;
  - incomplete unverified native mutation;
  - official rollback success;
  - official rollback failure; and
  - malformed or unreadable state.
- [ ] Add `tests/test_claude_reconciliation.py` for external `/login`,
  external `/logout`, a race with Sidekick activation, no silent import,
  and temporary external-active dashboard state.
- [ ] Add a test proving recovery never writes captured credential bytes.

### Implementation

- [ ] On startup, read actual native provider state before interpreting an
  incomplete journal.
- [ ] Complete the target commit when target identity is already proven.
- [ ] Record rollback when source identity is proven.
- [ ] Let another deliberate saved or unknown external identity win and
  reconcile selected state accordingly.
- [ ] When an incomplete Sidekick mutation produced an unverified identity,
  attempt rollback by officially provisioning the source managed authority
  into native Claude.
- [ ] If rollback cannot be proven, set
  `reconciliation_required`, block further Claude switching, retain truthful
  metrics, and show a repair action.
- [ ] Never restore stale credential bytes or overwrite an external official
  login merely to match the journal.

### Verify and commit

- [ ] Run recovery, reconciliation, activation, service restart, and
  persistence crash tests.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 9. Task 7 — Higher-Priority Credentials, Remote Control, and Sessions

**Commit:** `feat(claude): guard native session switching`

### Tests first

- [ ] Add `tests/test_claude_environment.py` for each higher-priority
  credential mode and combinations. Prove Sidekick reports the conflict and
  does not alter the parent environment.
- [ ] Add `tests/test_claude_remote_control.py` for inactive, active,
  unreadable, race-to-active, confirmation accepted, confirmation declined,
  and non-interactive refusal.
- [ ] Add session tests proving:
  - a new bare `claude` resolves the unchanged vendor executable;
  - an existing subscription session reads new auth on its next safe request;
  - an in-flight request remains on its original auth;
  - an explicitly environment-authenticated session stays outside switching;
  - a background/remote session reports actual support; and
  - native Windows reports unsupported.

### Implementation

- [ ] Detect cloud-provider mode, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, gateway,
  and other documented higher-priority modes before native activation.
- [ ] Do not unset, override, or persist any parent-shell value. Return a
  typed conflict with precise scope.
- [ ] Detect active Claude Remote Control through the exact supported local
  provider boundary.
- [ ] Require explicit confirmation only when the switch is proven to disrupt
  Remote Control. A non-interactive `use` command fails unless the caller
  supplied `--allow-remote-control-disconnect`; it never prompts.
- [ ] Keep existing sessions on next-safe-request semantics and never claim
  mid-request retargeting.
- [ ] Add support classification to doctor and sanitized dashboard state.

### Verify and commit

- [ ] Run environment, Remote Control, activation, CLI, session, and output
  tests.
- [ ] Run Ruff and `ty`.
- [ ] Verify no test or production code edits the calling process environment,
  then commit.

## 10. Task 8 — Maintenance, Metrics, and Direct OAuth Removal

**Commit:** `refactor(claude): remove duplicate refresh ownership`

### Tests first

- [ ] Update `tests/test_claude_refresh.py`,
  `tests/test_claude_activity.py`, `tests/test_heartbeat.py`, and
  `tests/test_usage_service.py` so:
  - selected Claude uses verified native authority;
  - inactive accounts use their private authorities;
  - all accounts remain due independently;
  - setup tokens receive lifetime/health checks but no refresh;
  - one failure does not stop another; and
  - stale metrics remain exact and timestamped.
- [ ] Add an architecture check rejecting Claude direct OAuth refresh
  endpoints and credential-bearing HTTP mutations.
- [ ] Add a test rejecting a fallback from official CLI failure to direct
  HTTP.

### Implementation

- [ ] Route all subscription maintenance through official private/native
  authority workflows.
- [ ] Observe and retain the active native generation before switching away.
- [ ] Collect usage and activity once per logical account. Choose the
  appropriate healthy credential mode without adding their totals.
- [ ] Preserve fixed setup-token lifetime and use `regenerate`, never
  `refresh`, in action state.
- [ ] Remove `OAUTH_REFRESH_ENDPOINT`, direct refresh request bodies,
  refresh-response token parsing used only by that call, and fallback logic
  from `providers/claude/provider.py`.
- [ ] Remove the deliberate macOS CLI-refresh skip.
- [ ] Keep direct HTTPS only for provider usage and activity endpoints that
  remain part of the established contract.
- [ ] Search production code for a second subscription credential writer and
  delete every obsolete path.

### Verify and commit

- [ ] Run:

```bash
rg -n \
  "platform\\.claude\\.com/v1/oauth/token|OAUTH_REFRESH_ENDPOINT" \
  src tests
uv run pytest \
  tests/test_claude_*.py \
  tests/test_heartbeat.py \
  tests/test_usage_service.py \
  tests/test_usage_activity.py
uv run python packaging/check_architecture.py
uv run ruff check src/ tests/
uv run ty check src/ tests/
```

- [ ] The search may match explicit negative architecture-test strings only.
  It must match no production refresh call.
- [ ] Commit after the complete Claude suite is green.

## 11. Task 9 — Claude Phase Gate

**Commit:** `test(claude): verify managed multi-account selection`

- [ ] Add one end-to-end fake scenario with two managed subscription
  accounts, one preserved setup token, selection A to B, outgoing retention,
  next-request session change, and maintenance of unselected A.
- [ ] Add one macOS scenario with two config-derived Keychain services,
  native selection, Keychain lock, stale metrics, unlock, and recovery.
- [ ] Add one setup-token-only scenario that starts official migration,
  cancels, preserves all state, retries, succeeds, and appears as one account.
- [ ] Add one external-login race scenario proving official provider choice
  wins without silent import.
- [ ] Add one complete secret matrix covering protected reads, child
  environments, argv, stdout, stderr, persistence, journals, Sidekick
  protocol, logs, errors, and representations.
- [ ] Run the full project gate from the foundation plan.
- [ ] Confirm ordinary `claude` path and symlink resolution remain unchanged
  in a synthetic installation test.
- [ ] Confirm no real provider or current-machine mutation occurred.
- [ ] Commit the phase evidence.

## 12. Claude Completion Gate

Do not begin the dashboard and rollout plan until all statements are true:

- [ ] Every selectable subscription account has one stable private profile.
- [ ] Linux and WSL use isolated protected files.
- [ ] macOS uses distinct config-derived Keychain services.
- [ ] Official Claude is the only subscription credential writer.
- [ ] Direct OAuth refresh no longer exists.
- [ ] Setup tokens remain fixed-lifetime, preserved, and measured.
- [ ] A dual-authority logical account appears and aggregates once.
- [ ] A healthy switch retains outgoing authority and proves target native
  identity.
- [ ] Target private authority remains maintainable after activation.
- [ ] Interrupted switches recover by provider read-back and official
  rollback.
- [ ] Keychain failure and plaintext fallback fail closed.
- [ ] Higher-priority credential modes are not overridden.
- [ ] Remote Control confirmation occurs only when disruption is proven.
- [ ] External official login wins without silent import.
- [ ] Every unselected account remains maintained and measured.
- [ ] Normal `claude` resolution is unchanged.
- [ ] No live current-machine migration has run.
