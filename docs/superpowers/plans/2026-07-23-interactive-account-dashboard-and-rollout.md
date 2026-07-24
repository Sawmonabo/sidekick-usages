# Interactive Account Dashboard and Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the normal TTY usage dashboard into the approved cursor-driven
Claude and Codex account selector, preserve one-shot automation, guide
non-admin service setup, and complete the real one-machine migration and
cross-account verification.

**Architecture:** Join cached usage and metrics with provider-verified
selection state by stable account ID, render the existing Rich wide/narrow
dashboard with one focus cursor, and lazily run a `prompt_toolkit` input
application only for TTY defaults. Dashboard actions use the authenticated
local supervisor protocol. Scripted `use` remains non-interactive. Live
rollout uses only the completed Sidekick CLI and official provider processes.

**Tech Stack:** Python 3.14, Rich, prompt_toolkit 3.0.52, Typer, the
foundation supervisor client, provider activation services, pytest 9,
Unix pseudoterminals, Ruff, `ty`, `uv`, generated Homebrew packaging, Linux,
WSL, macOS arm64, and macOS x64.

## Global Constraints

- Complete the foundation, Codex, and Claude plans first.
- The approved design is normative:
  `docs/superpowers/specs/`
  `2026-07-23-interactive-global-account-selection-design.md`.
- Preserve the current robot masthead, product copy, provider panels, account
  groups, usage columns, reset countdowns, activity totals, warnings, legend,
  wide layout, and narrow layout.
- The only persistent healthy-row addition is `›` immediately before the
  existing bullet for the focused row.
- Do not add `IN USE`, `ACTIVATING`, `MIGRATION REQUIRED`, or equivalent
  normal-state badges.
- Healthy rows have no extra status text. Progress appears only in the footer.
- One cursor is visible because only one provider is focused.
- Moving the cursor previews only. Enter activates or repairs. Esc restores
  focus to the actual active account.
- The cursor begins on provider read-back, never merely the last persisted
  choice.
- Unknown native identities appear as temporary external rows and are never
  silently imported or assigned saved-account metrics.
- Credential health, metrics freshness, and active state remain independent.
- Interactive mode requires both stdin and stdout TTYs and no
  `--no-interactive`.
- Redirected invocation, `check`, and `--no-interactive` render once and never
  read keys.
- `sidekick-usages use <provider> <label>` never prompts.
- The first dashboard is visible before service setup is requested.
- Guided setup asks once, installs without administrator rights, verifies
  readiness, and resumes the original action.
- Normal `claude` and `codex` paths and symlink targets must be unchanged
  before and after setup and migration.
- `prompt_toolkit` is imported only after TTY checks and first paint. The
  supervisor, workers, help, and non-interactive paths must not import it.
- No provider or credential operation runs in the input/render loop.
- Cached first paint must complete within 250 ms and local input-to-feedback
  p95 must target 50 ms on the documented reference machine.
- Automated tests use synthetic account labels and fake services. Live labels
  and provider identities never enter tracked captures or fixtures.
- The current-machine migration occurs only in Task 9, after all automated
  gates pass. It uses the CLI and official provider processes, never manual
  credential file or Keychain edits.
- Codex will perform the migration. The user is needed only for unavoidable
  provider browser, MFA, password, or consent.
- Commit after each numbered task with the listed Conventional Commit
  message. Do not push until explicitly authorized.

---

- **Status:** Approved; blocked on phases 1-3
- **Date:** 2026-07-23
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Planning baseline:** `dfde7d8c3b1855e2307ed2fc24fb8a72497ed39d`
- **Interactive dependency decision:** `prompt-toolkit==3.0.52`
- **Dependency evidence:** [PyPI prompt-toolkit 3.0.52][prompt-toolkit-pypi]
- **Required platforms:** Linux, WSL, macOS arm64, macOS x64
- **Previous phase:** `2026-07-23-claude-managed-auth-and-selection.md`
- **Completion record:**
  `docs/superpowers/completion/`
  `2026-07-23-interactive-global-account-selection.md`

[prompt-toolkit-pypi]: https://pypi.org/project/prompt-toolkit/3.0.52/

## 1. Final Interaction Contract

The wide dashboard retains its current structure:

```text
╭─ CLAUDE · 3 accounts ─────────────────────────────────────────────╮
│                                                                  │
│  › ●  work@example.test              max      0%     51%         │
│                                             2h 28m  4d 13h        │
│                                                                  │
│    ●  personal@example.test          max      0%     96%         │
│                                             1h 58m  15h 8m        │
│                                                                  │
│    ●  automation@example.test        max      0%     99%         │
│         Complete the official Claude login before using it.      │
│                                                                  │
╰──────────────────  1,106,429,559 tokens · since Dec 28, 2025 ───╯

 ↑/↓ or j/k move   Tab provider   Enter use   r refresh   ? help   q exit
```

The identities are synthetic.

Key behavior is exact:

| Key | Behavior |
| --- | --- |
| Up, `k` | Preview the previous row in the focused provider. |
| Down, `j` | Preview the next row in the focused provider. |
| Tab | Focus the other provider at its verified active row. |
| Enter | Activate or repair the previewed account. |
| Esc | Cancel preview and return to the verified active row. |
| `r` | Refresh the previewed account without selecting it. |
| `R` | Refresh every due account without changing selection. |
| `?` | Toggle concise keyboard help. |
| `q` | Exit normally. |
| Ctrl-C | Exit with code 130 after restoring the terminal. |

Claude is initially focused when it has rows. Otherwise the first non-empty
provider is focused. When no native account is active, the first row receives
navigation focus and the footer truthfully says no verified native login is
active.

## 2. Target File Map

Create:

- `usage/dashboard_models.py`: no-secret joined dashboard snapshot;
- `usage/dashboard_service.py`: cached metrics plus local account metadata;
- `usage/selection_render.py`: cursor, external row, and actionable row copy;
- `usage/footer_render.py`: keys, progress, confirmation, help, and errors;
- `usage/dashboard_render.py`: wide/narrow composition facade;
- `cli/dashboard_controller.py`: immutable cursor and preview transitions;
- `cli/interactive_input.py`: lazy prompt_toolkit application and keys;
- `cli/interactive_dashboard.py`: event loop joining input, service events,
  and Rich renders;
- `cli/service_setup.py`: one-time guided service installation;
- `cli/commands/use.py`: scriptable activation command; and
- `cli/commands/managed_migration.py`: resumable operator migration command.

Refactor rather than expand the current 792-line `usage/render.py` and
807-line `cli/context.py`. Keep `usage_overview` as the stable one-shot
facade, delegating shared panel construction to focused modules.

The no-secret dashboard view contains:

- one `UsageCheckResult` or cached equivalent;
- stable account IDs and local display labels;
- one provider runtime state per provider;
- credential health and action state per account;
- metrics freshness and observation time per account;
- cursor provider and row ID;
- temporary external rows;
- one transient footer state; and
- no credential, authority path, raw provider identity, or provider payload.

## 3. Task 1 — Cached Dashboard Read Model

**Commit:** `feat(usage): add provider selection dashboard state`

### Tests first

- [ ] Add `tests/test_dashboard_models.py` for:
  - usage joined by stable account ID;
  - selected account from provider read-back;
  - independent Claude and Codex active state;
  - missing metrics;
  - stale metrics with timestamp;
  - credential warning on an active account;
  - unknown external row;
  - no active identity;
  - deleted/renamed accounts; and
  - rejection of duplicate or mismatched provider IDs.
- [ ] Add `tests/test_dashboard_service.py` for cached first load, supervisor
  snapshot merge, service unavailable, partial account failure, external
  state, and no credential-authority read.
- [ ] Add an architecture test rejecting credential resolver, provider, HTTP,
  and secret persistence imports from dashboard models and rendering.
- [ ] Run the tests and confirm failure because the joined read model does not
  exist.

### Implementation

- [ ] Add immutable dashboard types and closed actionable states:
  `healthy`, `login_required`, `repair_required`,
  `setup_token_regeneration`, `metrics_stale`, `external_active`,
  `reconciliation_required`, `provider_unsupported`, and
  `service_unavailable`.
- [ ] Join account index, selected state, service state, and persisted metrics
  by stable account ID.
- [ ] Use provider read-back relation as the only active-account signal.
- [ ] Insert a temporary external row only when actual provider identity is
  unknown to Sidekick. Give it no saved metrics or implicit label.
- [ ] Preserve current exact usage and activity aggregation. A logical Claude
  account with two authorities contributes once.
- [ ] Keep stale last-known metrics visible with exact observation time and
  never silently promote them to current.
- [ ] Return a safe cached snapshot when the supervisor is unavailable.
  Actions remain disabled until readiness is restored.

### Verify and commit

- [ ] Run dashboard model/service, usage, activity snapshot, selected state,
  and architecture tests.
- [ ] Run Ruff and `ty`, inspect representations and fixtures for secrets,
  then commit.

## 4. Task 2 — Cursor-Aware Rich Wide and Narrow Rendering

**Commit:** `feat(render): add account selection cursor`

### Tests first

- [ ] Extend `tests/test_render.py` with exact wide renders for:
  - one cursor in Claude;
  - one cursor in Codex;
  - active row warning;
  - preview row;
  - setup-token login action;
  - rejected Codex repair action;
  - stale metrics;
  - external row;
  - no active account; and
  - transient footer progress.
- [ ] Add narrow render cases at every existing width boundary.
- [ ] Add assertions that healthy output contains none of:
  `IN USE`, `ACTIVATING`, `MIGRATION REQUIRED`, `CURRENT`, or a second
  cursor.
- [ ] Preserve existing exact masthead, panels, reset countdowns, totals,
  warning content, legend, and no-account behavior.
- [ ] Add resize cases that render the same state wide, narrow, then wide
  without losing cursor or active relation.

### Implementation

- [ ] Split shared panel construction out of the near-800-line renderer before
  adding cursor behavior.
- [ ] Reserve exactly two display cells for cursor or blank prefix before the
  existing bullet in interactive renders.
- [ ] Render cursor only in the focused provider.
- [ ] Render row details only for actionable or degraded state. Never render a
  normal active badge.
- [ ] Render switching, refresh, service setup, login wait, rollback, and
  completion progress only in the footer.
- [ ] Render the concise key footer by default and a bounded help footer when
  requested.
- [ ] Preserve one-shot rendering with no cursor and its current exit status.
- [ ] Keep display labels local to the CLI/render process. Do not add them to
  supervisor messages or logs.

### Verify and commit

- [ ] Run all render, branding, reset display, and activity render tests.
- [ ] Generate synthetic before/after wide and narrow captures and inspect
  alignment manually.
- [ ] Run Ruff and `ty`, confirm module line limits, then commit.

## 5. Task 3 — Lazy prompt_toolkit Input Controller

**Commit:** `feat(cli): add portable dashboard key input`

### Dependency gate

- [ ] Refresh the primary-source dependency evidence immediately before
  adoption. Record version, Python 3.14 import/runtime result, BSD license,
  wheel provenance, transitive `wcwidth`, release cadence, Linux/macOS
  behavior, and owned-code alternative in the approved design's dependency
  record.
- [ ] Pin `prompt-toolkit==3.0.52` only if the refreshed evidence and local
  Python 3.14 tests remain green. A changed current release requires an
  explicit design evidence update, not an unreviewed version substitution.

### Tests first

- [ ] Add `tests/test_dashboard_controller.py` for every key transition,
  provider focus, active-row restoration, preview cancellation, wrap or clamp
  behavior, external rows, row removal during preview, and post-activation
  cursor state.
- [ ] Choose clamped movement at the first and last row. Assert it explicitly
  so terminal auto-repeat cannot jump providers.
- [ ] Add `tests/test_interactive_input.py` using a fake input/output boundary
  for arrow sequences, `j`, `k`, Tab, Enter, Esc, `r`, `R`, `?`, `q`,
  Ctrl-C, resize, EOF, and unknown keys.
- [ ] Add import tests proving prompt_toolkit is absent after:
  - package import;
  - `--help`;
  - `--version`;
  - `check`;
  - redirected default invocation;
  - `--no-interactive`;
  - supervisor startup; and
  - worker startup.
- [ ] Add terminal restoration tests for normal exit, exception, signal,
  supervisor disconnect, malformed event, and worker crash.

### Implementation

- [ ] Add the direct dependency and regenerate `uv.lock` through the owning
  tool:

```bash
uv add "prompt-toolkit==3.0.52"
```

- [ ] Keep controller transitions infrastructure-free and deterministic.
- [ ] After TTY checks and initial Rich render, lazily import
  `prompt_toolkit.application.Application`,
  `prompt_toolkit.formatted_text.ANSI`, and the key-binding APIs.
- [ ] Render Rich to an ANSI string in memory and present it through one
  prompt_toolkit application. Invalidation redraws the current region rather
  than appending complete dashboards.
- [ ] Keep alternate-screen behavior off so the current dashboard remains in
  normal terminal scrollback after exit.
- [ ] Let prompt_toolkit own raw mode, key decoding, resize notification,
  signal-safe cleanup, and terminal restoration.
- [ ] Never execute provider work inside a key binding. Enqueue one typed
  action and update the footer immediately.
- [ ] Ignore Enter and Esc while one activation is in flight. `q` exits
  normally and Ctrl-C exits with code 130; neither kills a post-journal
  provider operation. The supervisor completes or recovers it, and the next
  dashboard launch reads the resulting provider state.
- [ ] Restore terminal state in one outer `finally` path before translating
  any error to a process exit.

### Verify and commit

- [ ] Run controller, input, import, render, help, and smoke tests.
- [ ] Run `uv run python -X importtime -m sidekick_usages --help` and inspect
  that prompt_toolkit is absent.
- [ ] Run packaging, Ruff, `ty`, and architecture checks, then commit.

## 6. Task 4 — Interactive Default Invocation and Scriptable `use`

**Commit:** `feat(cli): select accounts from the usage dashboard`

### Tests first

- [ ] Add `tests/test_cli_interactive_dashboard.py` for TTY default,
  `--only`, no accounts, one provider, both providers, service disconnect,
  activation success, activation rollback, reconciliation required, refresh,
  and exit codes.
- [ ] Add `tests/test_cli_use.py` for exact syntax:

```text
sidekick-usages use <provider> <label>
```

- [ ] Prove `use` never prompts and fails clearly when service install,
  provider login, migration, Remote Control confirmation, or reconciliation
  is required.
- [ ] Prove
  `sidekick-usages use claude <label> --allow-remote-control-disconnect`
  is the only non-interactive opt-in for a proven Remote Control disruption.
- [ ] Add tests proving redirected stdin, redirected stdout, `check`, and
  `--no-interactive` never construct the input controller.
- [ ] Preserve `--only` as an interactive single-provider filter when default
  invocation is a TTY; Tab is then a no-op.
- [ ] Preserve current one-shot failure exit calculation for `check`,
  redirected invocation, and `--no-interactive`.

### Implementation

- [ ] Add root `--no-interactive` without moving behavior into the
  registration-only Typer root.
- [ ] Make default dispatch exact:
  - both stdin and stdout TTY and interactive enabled: cached first paint then
    interactive controller;
  - otherwise: current one-shot usage workflow.
- [ ] Keep explicit `check` permanently one-shot.
- [ ] Register `use` through a cohesive command module and typed lazy context.
- [ ] Resolve provider plus exact label locally to a stable account ID, then
  send only provider and ID to the supervisor.
- [ ] Map service and provider events to footer states. Update selected
  state only from the completed provider-verified event.
- [ ] After successful activation, keep the cursor on the newly active row.
- [ ] On failure, restore cursor to actual provider read-back and preserve the
  other provider's state.
- [ ] Return exact actionable commands from non-interactive failures.

### Verify and commit

- [ ] Run CLI interactive, `use`, usage command, help, architecture, smoke,
  and render suites.
- [ ] Run Ruff and `ty`.
- [ ] Inspect command help ordering and one-shot output compatibility, then
  commit.

## 7. Task 5 — Guided Automatic Service Setup

**Commit:** `feat(cli): guide per-user service setup`

### Tests first

- [ ] Add `tests/test_cli_service_setup.py` for:
  - dashboard rendered before prompt;
  - one plain-language confirmation;
  - declined setup;
  - successful install and readiness;
  - install failure;
  - readiness timeout;
  - stale protocol upgrade;
  - WSL rescue setup;
  - macOS LaunchAgent setup;
  - native Windows unsupported;
  - resumed original activation; and
  - no repeated confirmation after verified setup.
- [ ] Prove no administrator command, password prompt, `sudo`, shell edit, or
  vendor executable mutation occurs.
- [ ] Prove non-interactive `use` does not install or prompt.

### Implementation

- [ ] On the first action requiring an absent supervisor, keep the dashboard
  visible and explain that one small per-user service maintains accounts and
  updates supported sessions.
- [ ] Ask once using the interactive controller.
- [ ] On approval, call the existing `DaemonManager` directly through a typed
  CLI service; do not shell out to the public Sidekick command.
- [ ] If a compatible service is already installed but unavailable, attempt a
  bounded user-level restart and readiness check before offering reinstall.
  Preserve cached metrics and the dashboard throughout.
- [ ] Stream sanitized installation, start, socket, provider, Codex broker,
  maintenance, restart, and legacy-schedule transition progress to the
  footer.
- [ ] Continue the original activation automatically after readiness. Do not
  require another Enter.
- [ ] On failure, preserve the dashboard and show one exact corrective action.
- [ ] Persist only successful setup acknowledgement tied to the installed
  service protocol generation. Re-prompt after a real incompatible reinstall,
  not every launch.

### Verify and commit

- [ ] Run guided setup, daemon lifecycle, platform backend, interactive CLI,
  and output-safety tests.
- [ ] Run Ruff, `ty`, and architecture checks, then commit.

## 8. Task 6 — Managed Migration Command, Warnings, and Doctor

**Commit:** `feat(cli): guide managed account migration`

### Tests first

- [ ] Register and test:

```text
sidekick-usages migrate managed-auth
```

- [ ] Add `tests/test_cli_managed_migration.py` for secret-safe preview,
  resumability, provider ordering, one-account failure continuation, browser
  wait, cancellation, service readiness, setup-token preservation, Codex
  independent login, and final all-account proof.
- [ ] Add exact warning-copy tests for:
  - Claude official login required;
  - Codex rejected login repair;
  - setup-token regeneration;
  - stale metrics and retry;
  - external login;
  - provider unsupported;
  - service unavailable;
  - switch rolled back; and
  - reconciliation required.
- [ ] Extend doctor tests for the complete approved service, platform,
  provider, private authority, native relation, metrics, queue, journal, and
  manual-action report.

### Implementation

- [ ] Add a resumable managed-auth migration coordinator that:
  1. validates and migrates the account index;
  2. ensures the service is installed and ready;
  3. migrates each Codex account independently;
  4. migrates each Claude account independently;
  5. preserves account-specific manual action without stopping later work;
  6. verifies all authorities and due state; and
  7. reports remaining actions without secrets.
- [ ] Keep this command interactive when provider login is required. It may
  accept already-authorized continuation but never accepts tokens as command
  arguments.
- [ ] Replace current generic token-expired copy with authority-specific
  actions. Do not display a persistent migration badge.
- [ ] Ensure warnings do not displace cursor meaning and do not imply stale
  metrics are current.
- [ ] Expand doctor through focused diagnostic modules rather than extending
  the current 756-line `doctor.py`.

### Verify and commit

- [ ] Run migration CLI, doctor, warning render, provider migration,
  persistence migration, and help tests.
- [ ] Run Ruff and `ty`, inspect output for real identities and secrets, then
  commit.

## 9. Task 7 — Pseudoterminal, Performance, Packaging, and Platform Gates

**Commit:** `test(cli): verify interactive dashboard resilience`

### Pseudoterminal coverage

- [ ] Add `tests/pty_support.py` using standard-library `pty`, selectors, and
  subprocess on Unix. Do not add `pexpect`.
- [ ] Add `tests/test_dashboard_pty.py` for:
  - first paint;
  - arrows and `j`/`k`;
  - Tab;
  - Enter;
  - Esc;
  - refresh keys;
  - help;
  - resize;
  - wide/narrow transition;
  - no-color;
  - service event while a key arrives;
  - Ctrl-C;
  - child crash;
  - no duplicated full dashboard; and
  - restored terminal echo and canonical mode.
- [ ] Run PTY integration on Linux and both macOS architectures in CI.
- [ ] Test WSL service and rescue generation automatically; retain real WSL
  stop/start for Task 9.

### Performance coverage

- [ ] Add `packaging/benchmark_dashboard.py` with synthetic account counts
  matching and exceeding the current machine.
- [ ] Measure cached first paint over at least 30 fresh processes and require
  p95 no more than 250 ms.
- [ ] Measure cursor input to visible render over at least 100 local events and
  target p95 no more than 50 ms.
- [ ] Measure steady supervisor RSS, idle CPU, worker exit, and Codex callback
  isolation using the foundation gates.
- [ ] Fail the architecture gate if prompt_toolkit enters supervisor,
  non-interactive, or help imports.

### Packaging and documentation

- [ ] Update `pyproject.toml`, `uv.lock`, wheel smoke, exact distribution
  inspection, and generated Homebrew dependency resources for
  prompt_toolkit 3.0.52 and `wcwidth`.
- [ ] Update Linux, macOS, and Windows CI so required Unix platforms run PTY
  tests while native Windows proves feature-disabled behavior.
- [ ] Update README command examples, keys, service setup, session coverage,
  unsupported modes, and uninstall behavior.
- [ ] Add synthetic before/after terminal captures to the completion record.

### Verify and commit

- [ ] Run:

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

- [ ] Inspect the wheel for all three entry points, prompt_toolkit, `wcwidth`,
  service templates, and absence of caches or credentials.
- [ ] Record benchmark environment and exact results in the completion record.
- [ ] Commit the gate and documentation changes.

## 10. Task 8 — Automated Release Acceptance Matrix

**Commit:** `test: verify global account selection acceptance`

- [ ] Encode all 24 design acceptance gates in
  `tests/test_global_account_selection_acceptance.py` using typed fakes and
  explicit evidence helpers.
- [ ] Prove normal vendor executable and symlink resolution is unchanged.
- [ ] Prove no wrapper, alias, function, PATH shim, or shell edit is produced.
- [ ] Prove one healthy Enter switches only the focused provider.
- [ ] Prove new and supported ongoing sessions change at their documented safe
  boundary while in-flight work remains unchanged.
- [ ] Prove every unselected account remains maintained and measured.
- [ ] Prove setup tokens remain fixed-lifetime and invalid Codex accounts use
  independent official login.
- [ ] Prove one account failure does not stop another.
- [ ] Prove external official login wins without silent import.
- [ ] Prove every interrupted switch commits verified state, officially rolls
  back, or blocks for reconciliation.
- [ ] Prove service install/uninstall requires no administrator rights and
  leaves provider logins untouched.
- [ ] Prove managed-authority v0.6 rollback fails before mutation.
- [ ] Run the complete gate twice from a clean test-state directory to catch
  non-idempotent setup or migration.
- [ ] Commit the acceptance evidence.

## 11. Task 9 — Current-Machine Migration and Live Verification

**Commit:** `docs(completion): record managed account rollout`

This is the only task authorized to mutate the current machine's Sidekick or
provider state. Execute it only after Tasks 1-8 and all three earlier plans are
green.

### Read-only preflight

- [ ] Confirm the worktree and built artifact are clean and exact.
- [ ] Record Sidekick, Claude, and Codex versions and absolute executable
  paths.
- [ ] Record vendor symlink targets and shell command resolution without
  changing them.
- [ ] Record the actual native identity relation for each provider without
  logging raw provider IDs.
- [ ] Inventory every saved logical account, credential kind, private
  authority health, metrics timestamp, heartbeat state, scheduler, service,
  and WSL rescue state.
- [ ] Record an owner-only, secret-safe recovery inventory. Do not copy token
  files or Keychain payloads.
- [ ] Run the complete automated gate once more.

### Install the exact implementation

- [ ] Build and smoke-test the wheel.
- [ ] Install the exact local project through the existing `uv tool`
  installation path:

```bash
uv tool install --force --reinstall .
```

- [ ] Verify `command -v sidekick-usages`, `claude`, and `codex` plus vendor
  symlink targets are unchanged except for the expected Sidekick package
  version.
- [ ] Start the default dashboard and capture a secret-safe before view with
  labels replaced by synthetic labels in tracked documentation.

### Transition the service

- [ ] Run the guided service setup from the dashboard or the existing daemon
  lifecycle command.
- [ ] Verify user-service readiness, socket peer proof, queue enrollment for
  every saved account, provider capability, Codex daemon/broker readiness, one
  truthful maintenance pass, and restart recovery.
- [ ] Remove the old periodic schedule only through the verified transition.
- [ ] Prove exactly one scheduler and one broker remain.
- [ ] On WSL, test Windows logon rescue, WSL stop/start, systemd recovery, and
  no duplicate maintenance.

### Migrate every account

- [ ] Run:

```bash
sidekick-usages migrate managed-auth
```

- [ ] For each Codex account, allocate the final private home, perform
  independent official login, verify identity and managed refresh, convert to
  sanitized metadata, retire the legacy credential only after success, and
  preserve metrics history.
- [ ] For each Claude account, allocate the final private profile, perform
  official subscription login, verify identity and protected storage,
  preserve any setup token, collect current metrics, and prove inactive
  maintenance.
- [ ] Continue after account-scoped failures and return later. Ask the user
  only when the provider requires browser, MFA, password, or consent.
- [ ] Never manually copy or edit an account, credential file, private auth
  bundle, or Keychain item.

### Verify global selection

- [ ] Preserve the deliberate current native selections before testing.
- [ ] Restart only Codex TUIs that predate official daemon enrollment.
- [ ] Select every Claude account from the dashboard and verify a new bare
  `claude` uses it.
- [ ] Select every Codex account from the dashboard and verify a new bare
  `codex` uses it.
- [ ] Verify supported existing sessions update on the next safe request.
- [ ] Verify in-flight requests are not retargeted.
- [ ] Verify all unselected accounts continue maintenance, usage, heartbeat,
  and metrics.
- [ ] Verify one provider selection never changes the other.
- [ ] Perform one external official login reconciliation check per provider.
- [ ] Verify rejected/expired warnings have been replaced by truthful current
  health or exact remaining action.
- [ ] Verify vendor executable paths, symlinks, shell configuration, and PATH
  remain unchanged.
- [ ] Exercise wide, narrow, resize, no-color, Ctrl-C, service restart,
  network loss/recovery, and WSL rescue behavior.

### Record and close

- [ ] Write the completion record using synthetic labels and no provider IDs,
  token hashes, credential paths, or raw provider output.
- [ ] Include exact commands, versions, gate results, performance results,
  remaining unsupported session modes, and service uninstall verification.
- [ ] Capture a sanitized after dashboard showing cursor-only selection and
  no normal-state badges.
- [ ] Confirm no credential-shaped value is tracked with:

```bash
git diff --check
git status --short
npm run lint:markdown
uv run pytest tests/test_docs.py
```

- [ ] Commit only the sanitized completion record and any already-reviewed
  documentation correction. Do not commit local service or credential state.

## 12. Final Release Gate

The feature is complete only when every item is true:

- [ ] All four implementation plans are complete.
- [ ] All 24 approved acceptance gates have automated evidence.
- [ ] Linux, WSL, macOS arm64, and macOS x64 have required automated and live
  evidence.
- [ ] Native Windows reports feature-disabled behavior accurately.
- [ ] The current dashboard layout remains recognizable and responsive.
- [ ] One cursor communicates normal active state without an `IN USE` label.
- [ ] TTY default is interactive; all one-shot paths remain non-blocking.
- [ ] Guided setup resumes the original action after one confirmation.
- [ ] Scripted `use` never prompts.
- [ ] Both Claude and Codex accounts remain fresh when unselected.
- [ ] Ordinary vendor commands remain vendor-owned and resolve unchanged.
- [ ] Supported existing sessions update at the next safe request.
- [ ] No credential secret crosses a forbidden boundary.
- [ ] The current machine has been migrated and verified.
- [ ] The tracked completion record is synthetic, secret-safe, and complete.

## 13. Approved-Spec Traceability

The four-plan suite covers every release gate from design Section 16:

| Gate | Primary implementation evidence |
| --- | --- |
| 1. Vendor executable resolution | Dashboard Tasks 8-9 |
| 2. No shell or command interception | Dashboard Tasks 5, 8-9 |
| 3. Approved cursor interaction | Dashboard Tasks 2-4, 7 |
| 4. One-shot paths do not block | Dashboard Tasks 3-4 |
| 5. No healthy-row selection labels | Dashboard Task 2 |
| 6. Account-specific warnings | Dashboard Tasks 2 and 6 |
| 7. One-Enter healthy switch | Provider activation tasks; Dashboard Task 4 |
| 8. Independent provider selection | Codex Task 6; Claude Task 5 |
| 9. New ordinary terminals | Provider session tests; Dashboard Task 9 |
| 10. Supported ongoing sessions | Codex Task 4; Claude Task 7 |
| 11. In-flight stability | Provider session tests; Dashboard Tasks 8-9 |
| 12. Unselected maintenance and metrics | Codex Task 7; Claude Task 8 |
| 13. Fixed setup-token tracking | Claude Tasks 4 and 8 |
| 14. Independent Codex repair | Codex Task 3 |
| 15. Per-account failure isolation | Foundation Tasks 5 and 8 |
| 16. External login reconciliation | Codex Task 7; Claude Task 6 |
| 17. Interrupt recovery | Foundation Task 3; provider recovery tasks |
| 18. Supervisor performance | Foundation Task 8; Dashboard Task 7 |
| 19. Required platform coverage | Foundation Task 6; Dashboard Tasks 7 and 9 |
| 20. Pre-mutation capability failure | Codex Task 1; Claude Task 1 |
| 21. Secret-leak matrix | Every phase gate; Dashboard Task 8 |
| 22. Guided install and clean uninstall | Foundation Tasks 6-7; Dashboard Task 5 |
| 23. Current-machine migration | Dashboard Task 9 |
| 24. Unsafe rollback refusal | Foundation Tasks 1 and 8 |
