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
- The implementation baseline is the exact official Claude Code `2.1.220`
  executable. Revalidate the executable version and immutable file provenance
  before every managed operation.
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
- Follow the foundation plan's lean-test contract. Reuse or replace existing
  Claude tests, default to at most two new coherent behavior tests per task,
  and add a third only for a separate security or crash-recovery invariant.
- Prove behavior once through the highest stable Claude boundary. Do not add
  a test per login error, journal phase, credential conflict, platform
  spelling, or state permutation; retain only cases that exercise distinct
  provider or operating-system behavior.
- The Claude phase gate maps existing task evidence and adds no duplicate
  end-to-end, secret-matrix, or platform-matrix suite.
- The Claude phase may add only
  `tests/test_claude_managed_runtime.py`; all other assertions extend
  existing Claude owner tests. This is a ceiling, not a target.
- Commit and push after each numbered task with the listed Conventional
  Commit message, as explicitly authorized for this implementation.

---

- **Status:** In progress; Claude Task 1 complete
- **Date:** 2026-07-23
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Planning baseline:** `dfde7d8c3b1855e2307ed2fc24fb8a72497ed39d`
- **Installed Claude baseline:** `2.1.220`
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

## 2. Target Ownership Map

Create cohesive owner packages rather than flat filename families or further
growth in `providers/claude/provider.py`:

- `platform/` owns reusable exact-executable provenance and host
  classification;
- `providers/claude/{models,types,errors,process,environment}.py` owns the
  shared Claude subprocess boundary and provider-wide contracts;
- `providers/claude/managed/` owns exact-version capability qualification,
  protected authority read-back, official login, and provider runtime
  observations;
- `providers/claude/schema/` remains the strict untrusted-data boundary;
- `credentials/claude/managed/` owns private-profile preparation,
  maintenance, migration, activation, reconciliation, and session policy;
  and
- cohesive Claude persistence schemas and transactions live under the
  existing `persistence/schema/` and `persistence/transactions/` owner
  packages, not as flat `claude_*` modules.

Refactor `providers/claude/credentials.py` into a narrow protected-envelope
reader. Remove platform and transition responsibilities after callers move.
Do not add re-export shims or backward-compatibility modules during the
one-machine migration.

## 3. Task 1 — Exact Binary, Stable Profiles, and Capability Gate

**Commit:** `feat(claude): add stable profile capability boundary`

### Tests first

- [x] Extend `tests/test_claude_provider_boundaries.py` with one supported
  scenario covering exact executable provenance, stable ID-derived and
  contained private profiles, rename stability, distinct accounts, and the
  required official auth capabilities.
- [x] Add one fail-closed table whose cases are only genuinely different
  gates: path escape, missing official login capability, and
  feature-disabled native Windows. Prove no login child or native mutation
  starts.
- [x] Do not create separate executable, profile, and capability permutation
  suites.

### Implementation

- [x] Resolve Claude once through `shutil.which`, require an absolute regular
  executable, capture `claude --version`, and retain immutable provenance for
  the operation.
- [x] Probe only documented command surfaces:

```bash
claude auth status
claude auth login --help
```

- [x] Parse the documented default JSON from `claude auth status`; do not
  depend on the artifact-accepted but undocumented `--json` option.
- [x] Combine command probes with version-pinned installed-binary observations
  required for config-specific storage. Keep the latter explicitly marked
  compatibility-sensitive.
- [x] Derive private profiles only in `paths.py` from stable Sidekick account
  IDs. A label or provider email never appears in the path.
- [x] Create profile directories with owner-only traversal permissions and
  validate every component before use.
- [x] Return a closed capability result identifying file-backed Linux/WSL,
  Keychain-backed macOS, and unsupported native Windows.
- [x] Disable switching before mutation when required auth, storage, identity,
  or official refresh-token provisioning capability is absent.

### Verify and commit

- [x] Run:

```bash
uv run pytest \
  tests/test_claude_provider_boundaries.py \
  tests/test_paths.py \
  tests/test_architecture.py
```

- [x] Run Ruff and `ty`, inspect path derivation and subprocess argv, then
  commit.

## 4. Task 2 — Linux, WSL, and macOS Protected Storage Read-Back

**Commit:** `feat(claude): verify private credential authorities`

### Tests first

- [ ] Extend the provider-boundary suite with one Linux/WSL protected-profile
  scenario proving the exact `CLAUDE_CONFIG_DIR`, owner-only regular-file
  read-back, identity binding, and fail-closed rejection of an unsafe file.
- [ ] Add one macOS Keychain scenario proving native and two config-derived
  service names, read-only bounded invocation on arm64 and x64, secret-safe
  output, and fail-closed behavior for a locked Keychain or plaintext
  fallback.
- [ ] Do not add one test per Keychain status, file error, architecture, or
  namespace component.

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

- [ ] Run the two protected-storage scenarios plus existing output-safety,
  filesystem, and architecture regressions they touch.
- [ ] Run Ruff and `ty`.
- [ ] Search production code for Keychain mutation commands. Only read-only
  lookup is allowed, then commit.

## 5. Task 3 — Official Private-Profile Maintenance

**Commit:** `feat(claude): maintain accounts through official login`

### Tests first

- [ ] Extend `tests/test_claude_refresh.py` with one two-account maintenance
  scenario using official login in each final private profile. Assert minimal
  child environment, no token in argv, same-identity read-back, independent
  authorities, no native activation, and continuation after one failure.
- [ ] Add one fail-closed official-login scenario for wrong identity or
  unverified generation, proving redacted output and unchanged prior
  authority. Existing subprocess tests continue covering generic timeout and
  child-exit mechanics.
- [ ] Do not duplicate the same workflow across Linux, WSL, arm64, and x64
  when the storage boundary tests already prove their distinct behavior.

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

- [ ] Run the two official-maintenance scenarios plus existing queue and
  credential-authority regressions they touch.
- [ ] Run Ruff and `ty`.
- [ ] Inspect child environments through synthetic fakes and confirm no
  parent credential variable leaks, then commit.

## 6. Task 4 — Setup-Token and Legacy Subscription Migration

**Commit:** `feat(claude): migrate accounts to managed profiles`

### Tests first

- [ ] In `tests/test_claude_managed_runtime.py`, add one two-account migration
  scenario covering a setup-token plus legacy subscription account and a
  second legacy account. Official private login preserves setup-token
  lifetime, metrics, and heartbeat state, aggregates one logical row, commits
  before legacy retirement, and continues when one account is canceled or
  mismatched.
- [ ] In the same file, add one interruption scenario after official login
  but before metadata commit. Recovery verifies identity and either completes
  the same transaction or requires reconciliation without losing either
  original authority.
- [ ] Fold existing setup-token save/restore assertions into the logical
  dual-authority contract and delete superseded replacement semantics.

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

- [ ] Run the two managed-migration scenarios plus existing setup-token,
  lifetime, transaction, activity, and output-safety regressions they touch.
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

- [ ] Extend `tests/test_claude_managed_runtime.py` with one healthy
  activation scenario that retains outgoing A, officially provisions B,
  verifies native identity, commits from read-back, requires one request when
  Remote Control is inactive, and leaves Codex state untouched.
- [ ] Add one interruption scenario at the externally meaningful boundary
  after native mutation and before commit. Recovery must serialize a
  concurrent retry and produce verified commit, official rollback, or
  reconciliation while keeping both private authorities usable.
- [ ] Do not force death after every internal write or enumerate equivalent
  preflight failures already covered by capability and storage tests.

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

- [ ] Run the two activation scenarios plus existing journal, provider-lock,
  Keychain, and output-safety regressions they touch.
- [ ] Run Ruff and `ty`.
- [ ] Review every native write path and confirm it launches official Claude,
  then commit.

## 8. Task 6 — Official Rollback and Reconciliation

**Commit:** `feat(claude): recover interrupted account switches`

### Tests first

- [ ] Extend the activation test boundary with one recovery scenario where
  provider read-back differs from the journal: official rollback succeeds
  once, and a failed rollback becomes reconciliation-required. Assert no
  captured credential bytes are written.
- [ ] Add one external-login race scenario to
  `tests/test_claude_managed_runtime.py` where the official provider state
  wins; a known account is related, an unknown account remains external, and
  neither is silently imported.
- [ ] Do not add separate cases for every possible prior native identity or
  logout spelling.

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

- [ ] Run the two recovery/reconciliation scenarios plus existing activation
  and persistence regressions they touch.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 9. Task 7 — Higher-Priority Credentials, Remote Control, and Sessions

**Commit:** `feat(claude): guard native session switching`

### Tests first

- [ ] Add one guard scenario covering a representative higher-priority
  credential and active Remote Control. Prove Sidekick changes no parent
  environment, requires the exact disruption approval, and refuses
  non-interactive activation without it.
- [ ] Add one session-boundary scenario proving bare `claude` still resolves
  to the vendor executable, a supported existing subscription session adopts
  the verified account only on its next safe request, and in-flight or
  explicitly environment-authenticated work is not retargeted.
- [ ] Do not enumerate equivalent environment combinations, confirmation
  outcomes, or unsupported session labels.

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

- [ ] Run the two guard/session scenarios plus existing CLI and output-safety
  regressions they touch.
- [ ] Run Ruff and `ty`.
- [ ] Verify no test or production code edits the calling process environment,
  then commit.

## 10. Task 8 — Maintenance, Metrics, and Direct OAuth Removal

**Commit:** `refactor(claude): remove duplicate refresh ownership`

### Tests first

- [ ] Extend one existing maintenance/usage scenario to prove selected Claude
  uses verified native state, inactive accounts use private authorities, all
  remain independently due, setup tokens receive fixed-lifetime checks but no
  refresh, and one failure leaves exact timestamped stale metrics while later
  accounts continue.
- [ ] Extend the architecture check once to reject direct OAuth refresh and
  credential-bearing HTTP mutation. Fold no-fallback proof into the official
  login failure scenario and adapt existing activity/heartbeat assertions
  without cloning the multi-account workflow.

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

This is a verification-only gate. It creates no duplicate end-to-end,
Keychain, setup-token, external-race, or secret matrix and requires no empty
commit.

- [ ] Map every Claude completion statement below to the smallest task test,
  foundation test, static check, or later authorized live check that proves
  it.
- [ ] Confirm the task scenarios collectively cover two managed accounts, one
  preserved setup token, A-to-B selection, outgoing retention, next-request
  session change, unselected maintenance, Keychain failure, and external
  provider choice.
- [ ] If a critical completion statement has no evidence, add one focused
  assertion to the nearest existing test. Do not create a Claude phase-gate
  test file.
- [ ] Run the full project gate from the foundation plan.
- [ ] Confirm ordinary `claude` path and symlink resolution remain unchanged
  through the existing packaging smoke boundary.
- [ ] Confirm no real provider or current-machine mutation occurred.

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
