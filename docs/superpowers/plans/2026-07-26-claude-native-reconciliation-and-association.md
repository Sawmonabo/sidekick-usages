# Claude Native Reconciliation and Association Correction Plan

> **Implementation status:** Not started. This plan is the closed execution
> contract derived from
> `2026-07-26-claude-native-reconciliation-proof.md`.

**Goal:** Correct Claude native-login truth, remove repeated false warnings,
support explicit saved-account association, and remain resilient to frequent
Claude and Codex CLI updates on Linux, WSL, macOS Arm64, and macOS x64.

**Architecture:** Keep provider-owned credentials separate from Sidekick-owned
private state. Establish an exact-profile Claude association through official
status, pair it with complete protected credential semantics, and commit only
a stable double observation. Keep executable versions diagnostic; qualify and
probe the stable vendor launcher's current target for each operation.

## Non-negotiable constraints

- Normal `claude` and `codex` remain their official vendor commands.
- Add no wrapper, alias, shell function, PATH shim, symlink replacement, or
  shell-startup edit.
- Add no provider release target, exact release equality, upper bound,
  release allowlist, or saved-version equality.
- A lower supported safety floor may reject inadequate old releases but may
  never reject a newer release merely because its version changed;
  behavioral capability probes are authoritative.
- Persist no "run provider release X" value in an account, service artifact,
  launcher, or worker. Release values are diagnostic evidence only.
- Freeze one resolved executable target only for the duration of one
  operation. A mid-operation launcher change fails closed; the next operation
  rediscovers and probes the new target.
- Only official Claude processes write or refresh Claude credentials.
- Never copy, edit, chmod, repair, migrate, replace, or remove native Claude
  files or Keychain items.
- Do not change or signal the current live Claude session during
  implementation or live Gate A.
- Do not associate or select a live Claude account without separate,
  exact-account approval.
- Keep first paint cache-only and keep all-account usage/activity lookups
  bounded and concurrent.
- Add no new test file or broad permutation suite.
- Keep imports at module top and constants directly below imports.
- Put new models in existing model owners and new types in existing type
  owners. Add no flat `*_types.py`, compatibility alias, or migration layer.
- Commit and push after each completed numbered implementation section.

## Closed design

### Provider-owned file read

Add one named, read-only `read_provider_owned()` platform operation. It is
separate from the existing Sidekick-private `read()` operation.

The operation:

- accepts only the direct `process_home / ".claude"` directory beneath the
  operating-system-discovered current-user home;
- opens the direct provider directory without following it;
- requires current-user ownership and no group/world write bits;
- accepts safe `0700` and `0755`;
- rejects `0775`, `0777`, symlinks, owner changes, and path-identity changes;
- qualifies the filesystem through the same held parent descriptor used for
  the read;
- opens the exact credential entry relative to that descriptor;
- requires a current-user-owned, owner-readable, private regular file;
- requires exactly one link and the same filesystem device;
- reads at most 1 MiB;
- revalidates parent, entry, and file metadata afterward;
- streams at most `_MAX_PROVIDER_DIRECTORY_ENTRIES = 4_096` names while
  retaining only constant-size exact-match and case-alias state;
- scans all accepted entries even after finding the exact name so a
  case-folded alias cannot be hidden later in the directory;
- raises the existing `TOO_LARGE` failure if entry 4,097 exists; and
- on macOS, requires APFS, `O_NOFOLLOW_ANY`, and no extended ACL on the
  direct parent or file.

The Windows implementation returns the existing unsupported failure. The
normal private reader and all Sidekick storage transactions remain unchanged.

### Exact-profile Claude authority proof

Create one cohesive provider proof owner under
`providers/claude/auth/proof/`. It is used by native and managed profiles.

For one exact `ClaudeProfile`, it:

1. reads the protected credential through the correct file or Keychain
   boundary;
2. captures generation, plan, scopes, expiry, and health;
3. calls the already-qualified executable's official `auth status` in that
   exact profile;
4. requires logged-in `claude.ai` and `firstParty` status;
5. requires nonempty provider-returned `email` and `orgId`;
6. hashes the length-delimited exact pair into one domain-prefixed opaque
   status association key;
7. rereads and requires the same generation, plan, scopes, access expiry,
   refresh expiry, health, and action; and
8. returns one complete `ClaudeAuthoritySnapshot`.

Claude's protected `claudeAiOauth` state determines request authorization.
Its separate global `oauthAccount` state supplies account/profile identity to
official status and the running UI. The complete snapshot binds the protected
semantic tuple to the status association key, so neither store can establish
or activate an account alone.

The email and organization fields are a release-observed, capability-probed
association surface, not a provider promise that they are immutable or
globally unique. Sidekick accepts the opaque key only after the user
explicitly associates one exact private profile. Every later use revalidates
it, and duplicate keys across private profiles fail closed. The key never
authorizes a label-based merge, import, overwrite, or identity replacement.
It remains a local profile association and does not claim the remote vault
design's immutable `provider_verified` assurance.

Embedded `tokenAccount` is no longer a second association scheme. There is no
legacy alias because this machine has no completed Claude managed-profile
association.

If status lacks or changes either required field, the proof fails closed. It
never uses a Sidekick label, plan, row, cursor, organization name, or
subscription name.

### Reconciliation

Retain the current two-observation, retry-on-change, final current-read, and
compare-and-swap sequence.

An active full snapshot:

- relates to a saved row only when exactly one explicitly associated private
  authority has the same current status association key and its private
  generation is valid;
- otherwise becomes one `EXTERNAL_ACTIVE` row; and
- never imports or renames a saved account.

Remove the now-unnecessary external-only identity and generation fields from
`ClaudeNativeObservation`. Active state owns exactly one full snapshot.

### Official native activation and existing sessions

The existing activation journal remains the transaction owner. A switch:

1. records the outgoing status association key and complete protected tuple;
   on Linux/WSL it also records the provider-visible credential `mtimeMs`;
2. uses official Claude to retain the outgoing account in its private
   profile;
3. proves the target private account with both status and protected state;
4. invokes official `claude auth login --claudeai` at the native default
   profile using only the target's leased refresh credential;
5. requires two stable native proofs showing the target status association
   key and target protected semantics; and
6. on Linux/WSL, requires official login to advance the provider-visible
   credential `mtimeMs`; and
7. commits selected state only after both stores and the platform propagation
   condition prove the target.

Sidekick does not write either provider store. If the official process updates
only secure credentials or only account-profile state, or if Linux/WSL
protected semantics change without an `mtimeMs` advance, the operation
remains uncommitted and enters the existing reconciliation path. Recovery
re-observes both and uses only official login to complete or roll back the
journaled transition. It never touches the credential file merely to change
its timestamp.

The installed Linux binary and matching official Darwin Arm64/x64 artifacts
prove ordinary model requests construct a client from current native
credentials rather than retaining a session-lifetime bearer token:

- Linux and WSL adopt the selected account on the next request after Sidekick
  observes official login advance the provider-owned credential file's
  `mtimeMs`.
- macOS adopts it on the first request after Claude's healthy-Keychain cache
  expires, within 30 seconds, or earlier when the previous token is rejected.
- A request already in flight keeps its original account.
- Explicit environment credentials and another `CLAUDE_CONFIG_DIR` remain
  isolated.
- Remote Control disconnects on a different-account login, so its existing
  approval guard remains mandatory.

No wrapper, process signal, restart, input injection, or release pin is
required for propagation.

### Version updates

Remove `authority.executable_version == snapshot.executable_version` from
`managed_authority_matches()`.

Keep `executable_version` only as "last successfully verified with"
diagnostic metadata. Update it when an actual authority commit already occurs;
do not add a compatibility write loop.

Keep:

- the stable `claude` and `codex` launcher paths;
- current-target qualification for each operation;
- behavioral/status/login/schema probes;
- before/after launcher-target verification;
- Codex current-CLI/current-daemon protocol equality; and
- automatic restart of an older official managed Codex daemon by the current
  qualified CLI.

The Codex daemon equality is per-operation protocol safety, not persisted
account identity. It has no upper release pin.

### Dashboard

Stop copying provider `UNREADABLE` and `UNSUPPORTED` state into every account
row.

Add one `provider_detail()` presentation helper over provider ID and the
existing `DashboardProvider.runtime_state`. Its exact copy is:

```text
UNREADABLE
  <provider> login could not be verified; account switching is paused.

UNSUPPORTED
  <provider> account verification is unavailable; saved metrics remain visible.
```

`<provider>` is `Claude Code` or `Codex CLI`. Render the result once with the
existing muted `UsageTextRole.ADVISORY`: after the provider's metric/model
column headings and before its first row in both wide and narrow layouts. Keep
account-specific login, repair, setup, service, and metrics details on the
affected account.

Healthy unassociated Claude state renders one external row. Setup-only help
appears only on the focused row while provider actions are enabled.

### Guided association

Add `ClaudeAssociationRequest(account_id)` and
`DashboardApplicationResult = int | ClaudeAssociationRequest` to the existing
dashboard controller model owner.

When Enter targets a focused Claude row with `SWITCH_SETUP_REQUIRED`:

1. controller and session activation return the typed request instead of
   daemon activation;
2. the Enter handler exits prompt-toolkit with that result;
3. the application returns the result only after its `finally` block closes
   the session and restores the terminal;
4. the dashboard entry point reloads the target by stable account ID,
   sanitizes its current label, and prompts exactly:

   ```text
   Connect '<label>' for future Claude switching?
   This keeps its setup token and does not change the active Claude account. [y/N]
   ```

5. default No, provider cancellation, and success each rebuild the dashboard;
6. on Yes, acquire `OperationAuthorityLock(account_id)`, reread that exact
   account, and invoke the existing one-account migration coordinator with
   identity establishment enabled;
7. let official interactive Claude login own terminal I/O and write only that
   private profile;
8. verify two complete stable authority proofs;
9. retain setup-token authority and metrics; and
10. rebuild the dashboard without submitting an activation intent.

The coordinator gains one account-ID entry point. The existing label-based
CLI presentation resolves a label to an account ID and delegates to the same
locked implementation; there is no second migration implementation or
compatibility alias.

Cancel changes nothing. Successful association does not write native Claude
and does not select another account.

## Execution DAG

```text
Task 1 -> Task 2
Task 2 -> Task 4
Task 2 -> Task 6
Task 3 -> Task 5
Task 4 -> Task 5
Task 5 -> Task 7
Task 6 -> Task 7
Task 7 -> Live Gate A
Live Gate A -> separately authorized Live Gate B
Live Gate B -> separately authorized Live Gate C
```

Tasks 1 and 3 may be developed in parallel isolated worktrees. Task 5's
presentation-only portion may proceed after its model contract is fixed.
Integrate, verify, commit, and push in numeric order 1 through 7. The
orchestrator owns conflict resolution, push verification, worktree cleanup,
and stale branch cleanup. No subagent may touch live provider state.

## Task 1 — Provider-owned read boundary

**Commit:** `fix(persistence): read provider credentials safely`

### Production changes

- [ ] Add `read_provider_owned()` to
  `persistence/platform/ports.py`.
- [ ] Implement the operation in the POSIX adapter with a named, dedicated
  code path rather than a public boolean policy.
- [ ] Bind filesystem qualification and read to the same held descriptor.
- [ ] Define `_MAX_PROVIDER_DIRECTORY_ENTRIES = 4_096` directly below imports
  in `persistence/platform/posix/namespace.py`.
- [ ] Replace unbounded exact-entry tuple creation with constant-memory
  descriptor-backed scanning that checks every accepted entry for an exact
  name or case-fold alias and returns `TOO_LARGE` on entry 4,097.
- [ ] Require the strict one-link provider file contract and 1 MiB caller
  limit.
- [ ] Revalidate the direct parent and entry after the read.
- [ ] Reuse the existing macOS descriptor ACL reader for parent and file.
- [ ] Keep APFS and `O_NOFOLLOW_ANY` mandatory on both macOS architectures.
- [ ] Return unsupported on native Windows.
- [ ] Add the read-only facade to
  `persistence/filesystem/reader.py`.
- [ ] Route only `_ClaudeNativeCredentialFiles` through it.
- [ ] Keep managed private profiles on `PrivateCredentialTree`.
- [ ] Add no writer, chmod, repair, migration, or fallback.

### Lean proof

Extend these two existing coherent boundary scenarios:

- `tests/providers/claude/test_managed_boundaries.py::`
  `test_file_profile_readback_is_exact_identity_bound_and_fail_closed`
  proves safe native `0755/0600`, held-descriptor filesystem qualification,
  exact-name/case-alias handling, and rejects writable parent, parent/file
  symlink, `0644`, two links, cross-device input, oversized input,
  entry 4,097, and concurrent identity or metadata change.
- `tests/providers/claude/test_managed_boundaries.py::`
  `test_keychain_readback_is_namespaced_bounded_and_fail_closed` proves APFS,
  `O_NOFOLLOW_ANY`, parent/file extended-ACL rejection, every bounded
  Keychain failure, plaintext fallback before and after access, and stable
  `/usr/bin/security` provenance.

Do not create a test function per platform, mode, or error.

### Gate

Run both node IDs above. The file scenario must pass on Linux CI; the Keychain
scenario must pass independently on macOS Arm64 and macOS x64 CI; live WSL
Gate A supplies the real WSL ext4/native-directory proof. Run targeted Ruff
and `ty`, the architecture gate, and `git diff --check`. Commit, push, and
verify local and remote `develop` agree before continuing.

## Task 2 — Exact-profile Claude association proof

**Commit:** `fix(claude): prove exact profile association`

### Production changes

- [ ] Create `providers/claude/auth/proof/service.py`; add no parallel types
  file.
- [ ] Rename the external-only status helper to
  `claude_status_association_key`; update callers directly and add no alias.
- [ ] Require nonempty email and org ID; remove empty-org hashing.
- [ ] Treat the key as release-observed association evidence established only
  by explicit exact-profile login; reject duplicate saved-profile keys.
- [ ] Keep the status output bound, timeout, strict JSON validation, and
  executable verification.
- [ ] Build the full snapshot from the status association key plus protected
  credential evidence.
- [ ] Treat the status key as proof of Claude's separate `oauthAccount`
  profile state and the protected tuple as proof of `claudeAiOauth` request
  authority; neither one may establish a complete authority alone.
- [ ] Compare the complete protected tuple `(generation, plan, scopes,
  access_expires_at, refresh_expires_at, health, action)` around status,
  return only post-read fields, and compare that tuple plus association key
  across complete outer observations.
- [ ] Use the same proof for native and managed profile reads.
- [ ] Preserve Keychain plaintext checks before and after secret access.
- [ ] Remove the special identityless/private-profile failure.
- [ ] Keep raw identity and credential values out of representations,
  exceptions, persistence, and logs.

### Lean proof

Extend:

`tests/credentials/claude/test_activation_recovery.py::`
`test_external_claude_login_wins_without_importing_unknown_identity`

- identityless credentials plus stable official status produce a full
  authority;
- the current unknown login becomes exactly one `EXTERNAL_ACTIVE` state;
- no saved label is selected;
- a missing, changed, or duplicate status key cannot associate;
- a status or any protected-semantic-field race commits nothing; and
- native credential bytes and saved accounts remain unchanged.

Extend the existing file-profile scenario from Task 1 only as needed to prove
the exact-profile status/credential composition. Add no new test.

### Gate

Run the activation-recovery and file-profile node IDs above, then targeted
Ruff, `ty`, architecture, and diff checks. Commit and push.

## Task 3 — Remove version identity

**Commit:** `fix(auth): keep CLI versions diagnostic`

### Production changes

- [ ] Remove executable-version equality from
  `managed_authority_matches()`.
- [ ] Keep plan, status association key, generation, and expiry equality.
- [ ] Keep version recording on successful authority commits.
- [ ] Search all production code for provider version equality, allowlists,
  or release-target paths.
- [ ] Retain Sidekick internal protocol/schema versions, explicit one-sided
  provider safety floors, and current-operation Codex CLI/daemon equality.
- [ ] Remove provider release targets, saved-version equality, upper bounds,
  and release allowlists.
- [x] Correct stale exact-version language in the 2026-07-23 Claude and Codex
  plans, global-selection design, and vault design during this planning
  checkpoint.
- [ ] Add no compatibility or background metadata rewrite.

### Lean proof

Extend
`tests/providers/claude/test_managed_boundaries.py::`
`test_supported_claude_boundary_freezes_executable_and_profiles`:

- save authority metadata with one synthetic version;
- observe the same association key, generation, plan, and expiry through a newer
  compatible version;
- require authority readiness to remain true;
- require a mid-operation launcher change to fail; and
- require fresh discovery of that launcher to succeed.

Retain the newer-target and managed-daemon restart assertions in
`tests/providers/codex/test_app_server.py::`
`test_versioned_codex_app_server_boundary_is_complete`. Do not add another
Codex test.

### Gate

Run those two exact node IDs, targeted static checks, the architecture gate,
and the provider-version search. Commit and push.

## Task 4 — Reconciliation and native activation truth

**Commit:** `fix(claude): reconcile verified native authority`

### Production changes

- [ ] Make active `ClaudeNativeObservation` own one complete snapshot.
- [ ] Remove the external-only identity/generation fields and update direct
  callers; add no compatibility alias.
- [ ] Preserve double observation, retry, current-read, and compare-and-swap.
- [ ] Relate a saved account only by one uniquely and explicitly associated
  private profile's current status association key.
- [ ] Preserve the external row when there is no unique verified relation.
- [ ] Preserve current selected state when proof is incomplete or changes.
- [ ] Do not mutate native Claude during reconciliation.
- [ ] Make activation and recovery reuse the same complete status/protected
  proof after every official native login.
- [ ] Journal the outgoing complete native proof, not a credential generation
  without its matching status/profile association.
- [ ] Commit a native switch only when official login leaves both the target
  protected request authority and target status/profile association stable.
- [ ] On Linux/WSL, capture provider-visible credential `mtimeMs` before
  every official native login, including recovery or rollback, and require a
  strictly later value afterward.
- [ ] Treat a one-store or mixed-store transition as reconciliation required;
  also treat changed protected semantics without Linux/WSL `mtimeMs`
  advancement as reconciliation required. Repair only through the existing
  official login exchange; never touch the file to advance its timestamp.

### Lean proof

Extend exactly these existing load-bearing scenarios:

- `tests/credentials/claude/test_activation_recovery.py::`
  `test_external_claude_login_wins_without_importing_unknown_identity`; and
- `tests/credentials/claude/test_activation.py::`
  `test_native_activation_retains_source_and_commits_verified_target`.

They must prove:

- known unique relation;
- unknown external relation;
- no guessed saved account;
- no mixed race commit; and
- repeat reconciliation converges to no change;
- official native activation produces matching secure-authority and
  account-profile proof for the exact target; and
- a one-store mismatch cannot commit selected state; and
- the Linux/WSL activation path cannot commit its existing-session guarantee
  unless the fake official login advances provider-visible `mtimeMs`.

### Gate

Run those two focused scenarios, targeted static/architecture checks, commit,
and push.

## Task 5 — One provider advisory

**Commit:** `fix(dashboard): scope provider auth warnings`

### Production changes

- [ ] Remove provider-runtime state fan-out from
  `CachedDashboardService._account()`.
- [ ] Add `provider_detail()` beside the existing row selection helpers.
- [ ] Render it once in wide and narrow provider panels.
- [ ] Reuse the existing muted advisory text role.
- [ ] Keep account-specific credential and metric warnings unchanged.
- [ ] Keep external-row and provider-only cursor truth unchanged.
- [ ] Keep cached first paint and keyboard footer unchanged.

### Lean proof

Extend only:

- `tests/dashboard/test_state.py::`
  `test_cached_dashboard_scopes_codex_broker_degradation`;
- `tests/usage/test_render.py::`
  `test_interactive_wide_render_preserves_dashboard_contract`; and
- `tests/usage/test_render.py::`
  `test_interactive_narrow_render_preserves_dashboard_contract`.

Require one provider advisory, zero copied reconciliation states on saved
rows, and unchanged row-specific warnings. Add no screenshot suite or new
test.

### Gate

Run those three exact node IDs, the dashboard benchmark once, targeted static
and architecture checks, commit, and push.

## Task 6 — Guided saved-account association

**Commit:** `feat(claude): connect selected saved account`

### Production changes

- [ ] Add `ClaudeAssociationRequest` and `DashboardApplicationResult` to the
  existing dashboard controller models.
- [ ] Propagate the typed request through controller, session port, session,
  Enter handler, prompt-toolkit application, and dashboard entry point.
- [ ] Return it only for focused setup-only Claude Enter; ordinary activation
  remains in its existing action owner.
- [ ] Exit the TUI with the request and close the session before prompting or
  starting any interactive login.
- [ ] Reload the exact target by account ID and use the exact default-No copy
  in the closed design.
- [ ] Add one account-ID migration entry, acquire the account lock before
  reread, and reuse one migration implementation with explicit identity
  establishment.
- [ ] Rebuild the dashboard after No, provider cancellation, or success.
- [ ] Preserve setup authority, metrics, every other account, native state,
  and active Claude processes.
- [ ] Do not auto-select the associated account.

### Lean proof

Extend:

- `tests/dashboard/test_state.py::`
  `test_dashboard_controller_journey_preserves_verified_truth`;
- `tests/dashboard/test_actions.py::`
  `test_managed_auth_migration_resumes_without_exposing_secrets`; and
- `tests/credentials/claude/test_migration.py::`
  `test_two_account_migration_preserves_dual_authority_and_metrics`.

Require one typed account-ID target, terminal restoration before the exact
confirmation/login, default-No cancellation with no side effect, no daemon
activation for setup-only Enter, exact private-profile login, stable status
association proof, setup preservation, unchanged native sentinel, and
secret-free output. Add no PTY or live-provider test.

### Gate

Run those three exact node IDs and targeted static/architecture checks.
Commit and push. Do not run live association.

## Task 7 — Release proof and cleanup

**Commit:** `docs(claude): record reconciliation acceptance`

### Local gate

- [ ] Run exactly these existing load-bearing scenarios:

  ```text
  tests/providers/claude/test_managed_boundaries.py::test_file_profile_readback_is_exact_identity_bound_and_fail_closed
  tests/providers/claude/test_managed_boundaries.py::test_keychain_readback_is_namespaced_bounded_and_fail_closed
  tests/providers/claude/test_managed_boundaries.py::test_supported_claude_boundary_freezes_executable_and_profiles
  tests/providers/codex/test_app_server.py::test_versioned_codex_app_server_boundary_is_complete
  tests/credentials/claude/test_activation_recovery.py::test_external_claude_login_wins_without_importing_unknown_identity
  tests/credentials/claude/test_activation.py::test_native_activation_retains_source_and_commits_verified_target
  tests/dashboard/test_state.py::test_cached_dashboard_scopes_codex_broker_degradation
  tests/usage/test_render.py::test_interactive_wide_render_preserves_dashboard_contract
  tests/usage/test_render.py::test_interactive_narrow_render_preserves_dashboard_contract
  tests/dashboard/test_state.py::test_dashboard_controller_journey_preserves_verified_truth
  tests/dashboard/test_actions.py::test_managed_auth_migration_resumes_without_exposing_secrets
  tests/credentials/claude/test_migration.py::test_two_account_migration_preserves_dual_authority_and_metrics
  ```

- [ ] Run Ruff and `ty` on changed source/tests.
- [ ] Run `packaging/check_architecture.py`.
- [ ] Run `git diff --check`.
- [ ] Run the dashboard benchmark once.
- [ ] Search for function-local imports, late globals, duplicate association
  helpers, provider release targets/equality/upper bounds/allowlists,
  wrappers, aliases, and compatibility layers.
- [ ] Confirm no new test file or test function was added.
- [ ] Confirm cached-first and concurrent lookup code is unchanged except
  where a reviewed correction requires it.

Do not run the full pytest suite locally on WSL.

### CI gate

- [ ] Push the checkpoint.
- [ ] Require Linux x64 CI green.
- [ ] Require macOS Arm64 CI green.
- [ ] Require macOS x64 CI green.
- [ ] Require exact-wheel smoke green on those platforms.
- [ ] Require Windows regression CI green with Claude switching still
  disabled.
- [ ] Resolve every review finding before live proof; no waiver or open
  review point remains.

### Live Gate A

- [ ] Record only redacted official status, stable launcher, active Claude
  PIDs/start times, and native parent/file metadata.
- [ ] Reinstall through the Sidekick CLI.
- [ ] Start the dashboard and allow read-only reconciliation.
- [ ] Require launcher, official status association key, PIDs, and credential
  metadata unchanged across the immediate reconciliation observation.
- [ ] Require the production path and focused boundary proof to expose no
  native credential writer, chmod, repair, login, logout, or activation call.
- [ ] Require one external or explicitly associated active relation.
- [ ] Require no repeated account-row reconciliation warnings.
- [ ] Allow one natural scheduled cycle and repeat the proof.
- [ ] If an independent official Claude process naturally refreshes the
  credential during either observation, classify the run as concurrent and
  repeat it after state stabilizes; do not attribute or suppress that refresh.
- [ ] Record the evidence in the completion document.

Do not press Enter on Claude, run login, select an account, restart Claude, or
signal its processes. Gate A does not read, hash, copy, or claim equality of
credential bytes.

### Live Gate B

Gate B remains blocked by authorization, not by design. Run it only after the
user names one exact account:

- [ ] associate that one private profile;
- [ ] prove setup authority and metrics remain;
- [ ] prove native Claude and active processes remain unchanged;
- [ ] prove every other account remains unchanged; and
- [ ] wait for a separate explicit Enter before any different-account switch.

### Live Gate C

Gate C remains separately blocked by account-switch authorization. Run it
only after the user names the associated source and target accounts and
approves any detected Remote Control disruption:

- [ ] record redacted native status, active Claude PIDs/start times, and the
  target's verified private association; on Linux/WSL, also record the native
  credential's provider-visible `mtimeMs`;
- [ ] press Enter once on the healthy associated target;
- [ ] require the dashboard cursor and native official status to prove that
  exact target while active Claude PIDs/start times remain unchanged;
- [ ] on Linux/WSL, require official login to advance `mtimeMs`; a missing
  advance fails without a manual file touch;
- [ ] require a new ordinary `claude` process to use the target;
- [ ] require one already-running ordinary subscription session to use the
  target on its next Linux/WSL request after the observed `mtimeMs` advance,
  or first healthy-macOS request after Claude's 30-second Keychain cache
  bound;
- [ ] require any request already in flight to retain its original account;
  and
- [ ] leave the approved target selected unless the user separately approves
  another switch.

Do not run Gate C against explicit environment authentication, a different
`CLAUDE_CONFIG_DIR`, or Remote Control without the required approval. Do not
restart, signal, or inject input into provider processes.

## Completion criteria

- [ ] Safe provider-owned `0755/0600` state reconciles on Linux and WSL.
- [ ] macOS Arm64 and x64 pass APFS, ACL, Keychain, and plaintext-fallback
  gates.
- [ ] Official exact-profile status is the sole Claude runtime association
  surface.
- [ ] Email and org ID are both required for the capability-probed opaque key;
  labels never participate and duplicate keys fail closed.
- [ ] The association key and complete protected semantic tuple are stable
  across repeated observations.
- [ ] An unassociated native login renders once as external.
- [ ] Provider failure renders once, not once per account.
- [ ] Setup-only Enter performs association after TUI teardown.
- [ ] Association preserves setup metrics and does not select native Claude.
- [ ] Native activation commits only after the official process leaves
  matching target secure-authority and account-profile truth.
- [ ] Linux/WSL next-request propagation requires an observed official-login
  `mtimeMs` advance; healthy-macOS propagation retains Claude's bounded cache.
  Sidekick adds no wrapper, manual timestamp touch, or process manipulation.
- [ ] Compatible provider CLI updates do not invalidate accounts.
- [ ] Mid-operation launcher changes still fail closed.
- [ ] Normal `claude` and `codex` commands remain vendor commands.
- [ ] First paint remains cache-only.
- [ ] All-account lookups remain bounded and concurrent.
- [ ] No new test file, broad matrix, compatibility layer, or duplicated
  policy exists.
- [ ] Linux, WSL, macOS Arm64, and macOS x64 evidence is complete.
- [ ] Every review finding is resolved.
- [ ] Every implementation commit is pushed and remote `develop` matches
  local `develop`.
