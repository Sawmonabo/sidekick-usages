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
dashboard with one focus cursor, then replace the cached-first CLI process
image with a dedicated interactive entry point whose `prompt_toolkit` imports
are static and top-level. One short-lived lookup worker submits every account
before awaiting results and uses bounded threads rather than one process per
account. Dashboard actions use the authenticated local supervisor protocol.
Scripted `use` remains non-interactive. Live rollout uses only the completed
Sidekick CLI and official provider processes.

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
- After TTY checks and first paint, `os.execve` replaces the launcher with a
  dedicated interactive process image. Only that entry point reaches static,
  top-level `prompt_toolkit` imports. The supervisor, workers, help, and
  non-interactive paths must not reach them.
- No provider or credential operation runs in the input/render loop.
- After cached first paint, submit every saved-account lookup before awaiting
  any result. Use one short-lived global worker with a measured, bounded thread
  wave whose cap covers the current saved-account population. Never serialize
  first-load work by provider or account and never create one operating-system
  process per account. Mutations remain in the existing isolated worker
  boundary.
- Cached first paint must complete within 250 ms and local input-to-feedback
  p95 must target 50 ms on the documented reference machine.
- Measure peak memory and first complete refresh for the real saved-account
  count. Concurrency is accepted only when it stays inside the supervisor
  memory gate and every account begins without avoidable provider-level
  serialization.
- Automated tests use synthetic account labels and fake services. Live labels
  and provider identities never enter tracked captures or fixtures.
- Follow the foundation plan's lean-test contract. Reuse or replace current
  render, CLI, and daemon tests, default to at most two new coherent behavior
  tests per task, and add a third only for a separate terminal-restoration or
  security invariant.
- Keep only one representative wide render, one representative narrow
  render, one main PTY journey, and one forced-cleanup PTY journey. Do not
  snapshot every warning, width, key, or service state.
- Release acceptance is a traceability review over existing focused tests,
  benchmarks, packaging checks, and the authorized live rollout. It must not
  create a second 24-gate acceptance test suite.
- The dashboard phase may add only `tests/test_dashboard.py`,
  `tests/test_dashboard_pty.py`, and the non-test helper
  `tests/pty_support.py`; all render assertions extend `tests/test_render.py`.
  This is a ceiling, not a target.
- The current-machine migration occurs only in Task 9, after all automated
  gates pass. It uses the CLI and official provider processes, never manual
  credential file or Keychain edits.
- Earlier Sidekick layouts and scheduler names are not runtime inputs. Task 5
  installs only the clean-break service. Task 9 uses the still-installed old
  Sidekick CLI to remove its own schedule before the clean-break install.
- Codex will perform the migration. The user is needed only for unavoidable
  provider browser, MFA, password, or consent.
- No live action may change the selected Claude account without a separate,
  just-in-time approval naming that action and target. General approval of
  this plan or Task 9 does not satisfy that gate.
- Commit and push after each numbered task with the listed Conventional Commit
  message, as already authorized for this implementation.

---

- **Status:** Automated implementation and local gate complete;
  cross-platform validation and current-machine rollout pending
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

Create cohesive owner packages:

- `usage/dashboard/{models,service}.py`: no-secret cached dashboard state;
- `usage/lookup/{models,service,wave}.py`: one account lookup and the bounded
  submit-all wave;
- `usage/presentation/dashboard/{overview,selection,footer}.py`: shared
  wide/narrow panels, cursor, actionable copy, keys, progress, and errors;
- `cli/dashboard/{launch,controller,input,application,setup}.py`: lean
  `execve` planning, immutable transitions, static prompt input, interactive
  orchestration, and guided service setup;
- `cli/contexts/dashboard/{snapshot,runtime}.py`: passive cached source and
  launcher composition with no provider, credential, HTTP, or maintenance
  graph;
- `entrypoints/dashboard.py`: dedicated interactive process image;
- `entrypoints/usage_lookup.py`: provider-heavy global lookup wave;
- `cli/commands/use.py`: scriptable activation command; and
- `cli/commands/migration.py`: resumable operator migration command.

Refactor rather than expand large presentation or context modules. Keep
`usage_overview` as the stable one-shot facade, delegating shared panel
construction to focused owners. Add a passive account-index reader that
decodes only secret-free metadata; cached composition must not open credential
bundles.

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

- [x] In `tests/test_dashboard.py`, add one joined-snapshot test with
  independent Claude and Codex read-back, stable-ID usage, one stale metric,
  one actionable warning, and one unknown external row. Rename one saved
  account in the same scenario to prove labels are not identity.
- [x] In the same file, add one degraded-cache scenario for unavailable
  supervisor and partial account failure. Prove cached metrics remain
  truthful, actions disable, and the read model cannot obtain credential
  authority.
- [x] Extend the existing architecture rule without adding another test
  function. Do not create separate model and service permutation suites.
- [x] Run the tests and confirm failure because the joined read model does not
  exist.

### Implementation

- [x] Add immutable dashboard types and closed actionable states:
  `healthy`, `login_required`, `repair_required`,
  `setup_regeneration_required`, `metrics_stale`, `external_active`,
  `reconciliation_required`, `provider_unsupported`, and
  `service_unavailable`.
- [x] Join account index, selected state, service state, and persisted metrics
  by stable account ID.
- [x] Read cached state through a passive, secret-free account index and one
  bulk decode of each usage and activity snapshot. Do not compose providers,
  HTTP, credential authorities, or maintenance before first paint.
- [x] Render cached state first, then start one short-lived global lookup
  worker. Inside that worker, submit every saved account across both providers
  to one bounded thread wave before awaiting any result. The measured worker
  cap must cover the current six-account population without creating one
  process, pool, or provider queue per account.
- [x] Give each account task one operation-scoped credential lease. Fetch
  Codex usage and activity through that same lease, and submit Claude local
  activity as part of the same global wave.
- [x] Make pooled HTTP initialization and shutdown safe for concurrent account
  tasks. Do not share unsynchronized lazy transport state or allocate a full
  transport stack per account.
- [x] Publish immutable, secret-free results independently as each account
  completes. Preserve deterministic provider and account display order with a
  fixed ordinal, and isolate one account failure from every other result.
- [x] Let the worker owner serialize or batch snapshot updates. Lookup threads
  must not mutate shared account or snapshot documents.
- [x] Use provider read-back relation as the only active-account signal.
- [x] Insert a temporary external row only when actual provider identity is
  unknown to Sidekick. Give it no saved metrics or implicit label.
- [x] Preserve current exact usage and activity aggregation. A logical Claude
  account with two authorities contributes once.
- [x] Keep stale last-known metrics visible with exact observation time and
  never silently promote them to current.
- [x] Return a safe cached snapshot when the supervisor is unavailable.
  Actions remain disabled until readiness is restored.

### Verify and commit

- [x] Run the two dashboard-state scenarios plus existing usage, activity,
  selected-state, and architecture regressions they touch.
- [x] Record one timing trace proving all saved accounts begin in the same
  bounded wave before any result is awaited, a slow account does not delay
  completed rows, the single child is reaped, and peak memory stays within the
  supervisor gate. Extend the joined-snapshot scenario; do not add a separate
  concurrency test or performance matrix.
- [x] Run Ruff and `ty`, inspect representations and fixtures for secrets,
  then commit.

## 4. Task 2 — Cursor-Aware Rich Wide and Narrow Rendering

**Commit:** `feat(render): add account selection cursor`

### Tests first

- [x] Extend `tests/test_render.py` with one representative wide render that
  contains both providers, exactly one cursor, one healthy row, one
  actionable warning, stale metrics, an external row, and footer progress.
- [x] Add one representative narrow render of the same state. Assert both
  preserve the existing masthead, panels, totals, and reset meaning while
  containing none of:
  `IN USE`, `ACTIVATING`, `MIGRATION REQUIRED`, `CURRENT`, or a second
  cursor.
- [x] Do not add a snapshot for every warning, provider focus, width
  boundary, or resize step. Existing rendering tests continue covering
  unchanged masthead and no-account behavior.

### Implementation

- [x] Split shared panel construction out of the near-800-line renderer before
  adding cursor behavior.
- [x] Reserve exactly two display cells for cursor or blank prefix before the
  existing bullet in interactive renders.
- [x] Render cursor only in the focused provider.
- [x] Render row details only for actionable or degraded state. Never render a
  normal active badge.
- [x] Render switching, refresh, service setup, login wait, rollback, and
  completion progress only in the footer.
- [x] Render the concise key footer by default and a bounded help footer when
  requested.
- [x] Preserve one-shot rendering with no cursor and its current exit status.
- [x] Keep display labels local to the CLI/render process. Do not add them to
  supervisor messages or logs.

### Verify and commit

- [x] Run the two render scenarios plus existing unchanged branding, reset,
  and activity render regressions.
- [ ] Generate synthetic before/after wide and narrow captures and inspect
  alignment manually.
- [x] Run Ruff and `ty`, confirm module line limits, then commit.

## 5. Task 3 — Process-Isolated prompt_toolkit Input Controller

**Commit:** `feat(cli): add portable dashboard key input`

### Dependency gate

- [x] Refresh the primary-source dependency evidence immediately before
  adoption. Record version, Python 3.14 import/runtime result, BSD license,
  wheel provenance, transitive `wcwidth`, release cadence, Linux/macOS
  behavior, and owned-code alternative in the approved design's dependency
  record.
- [x] Pin `prompt-toolkit==3.0.52` only if the refreshed evidence and local
  Python 3.14 tests remain green. A changed current release requires an
  explicit design evidence update, not an unreviewed version substitution.

### Tests first

- [x] Extend `tests/test_dashboard.py` with one infrastructure-free controller
  journey covering clamped movement, provider focus, preview-only movement,
  Esc restoration, Enter dispatch, refresh actions, help, and
  post-activation cursor state. Do not test key aliases separately when they
  map to the same action.
- [x] Fold process routing into Task 4's single CLI scenario. Do not add a
  second import-routing test.
- [x] Leave real key decoding and terminal restoration to the two PTY
  scenarios in Task 7; do not duplicate them with fake-input cases.

### Implementation

- [x] Add the direct dependency and regenerate `uv.lock` through the owning
  tool:

```bash
uv add "prompt-toolkit==3.0.52"
```

- [x] Keep controller transitions infrastructure-free and deterministic.
- [x] Keep `prompt_toolkit` imports static in the initial top-level import
  block of the dedicated interactive process graph. Do not add a function
  import, dynamic loader, forwarding module, or architecture exception.
- [x] After TTY checks and initial Rich render, call `os.execve` with the same
  absolute `sys.executable` to replace the launcher with the dedicated
  interactive entry point. Pass only safe routing options in argv; the new
  process re-reads persisted secret-free dashboard state.
- [x] Prove help, version, supervisor, worker, redirected output, explicit
  `check`, and `--no-interactive` cannot reach the interactive import graph.
  If `execve` fails, restore the cursor below the cached frame before
  displaying the error.
- [x] Render Rich to an ANSI string in memory and present it through one
  prompt_toolkit application. Invalidation redraws the current region rather
  than appending complete dashboards.
- [x] Keep alternate-screen behavior off so the current dashboard remains in
  normal terminal scrollback after exit.
- [x] Let prompt_toolkit own raw mode, key decoding, resize notification,
  signal-safe cleanup, and terminal restoration.
- [x] Never execute provider work inside a key binding. Enqueue one typed
  action and update the footer immediately.
- [x] Ignore Enter and Esc while one activation is in flight. `q` exits
  normally and Ctrl-C exits with code 130; neither kills a post-journal
  provider operation. The supervisor completes or recovers it, and the next
  dashboard launch reads the resulting provider state.
- [x] Restore terminal state in one outer `finally` path before translating
  any error to a process exit.

### Verify and commit

- [x] Run the controller journey and Task 4's CLI routing scenario plus
  existing render, help, and smoke regressions they touch.
- [x] Run `uv run python -X importtime -m sidekick_usages --help` and inspect
  that prompt_toolkit is absent. Inspect the import graph to prove only the
  dedicated interactive entry point reaches it.
- [x] Run packaging, Ruff, `ty`, and architecture checks, then commit.

## 6. Task 4 — Interactive Default Invocation and Scriptable `use`

**Commit:** `feat(cli): select accounts from the usage dashboard`

### Tests first

- [x] Extend `tests/test_dashboard.py` with one CLI routing test proving TTY
  default paints cached state before `execve`, `--only` constrains focus, and
  redirected input/output, `check`, `--no-interactive`, help, and supervisor
  startup never cross the interactive entry point and retain their intended
  one-shot behavior and exit calculation.
- [x] Add one scriptable command test in the same file for the exact syntax:

```text
sidekick-usages use <provider> <label>
```

- [x] In that command test, prove `use` never prompts, returns one actionable
  failure when preparation is required, and accepts
  `sidekick-usages use claude <label> --allow-remote-control-disconnect`
  only for a proven Remote Control disruption.
- [x] Do not build account-count, service-error, provider-error, or exit-code
  matrices already covered by the controller and provider boundaries.

### Implementation

- [x] Add root `--no-interactive` without moving behavior into the
  registration-only Typer root.
- [x] Make default dispatch exact:
  - both stdin and stdout TTY and interactive enabled: cached first paint then
    interactive controller;
  - otherwise: current one-shot usage workflow.
- [x] Keep explicit `check` permanently one-shot.
- [x] Register `use` through a cohesive command module and a statically
  imported typed context boundary.
- [x] Resolve provider plus exact label locally to a stable account ID, then
  send only provider and ID to the supervisor.
- [x] Map service and provider events to footer states. Update selected
  state only from the completed provider-verified event.
- [x] After successful activation, keep the cursor on the newly active row.
- [x] On failure, restore cursor to actual provider read-back and preserve the
  other provider's state.
- [x] Return exact actionable commands from non-interactive failures.

### Verify and commit

- [x] Run the two CLI scenarios plus existing usage, help, architecture,
  smoke, and render regressions they touch.
- [x] Run Ruff and `ty`.
- [x] Inspect command help ordering and one-shot output compatibility, then
  commit.

## 7. Task 5 — Guided Automatic Service Setup

**Commit:** `feat(cli): guide per-user service setup`

### Tests first

- [x] Extend `tests/test_dashboard.py` with one guided-setup journey: render
  first, ask once, install the user-level service, verify readiness, resume
  the original activation, and skip repeat confirmation. Assert no
  administrator command, password prompt, shell edit, or vendor executable
  mutation occurs.
- [x] Add one refusal/failure journey in the same file proving decline or
  bounded setup failure leaves the dashboard usable, preserves state, and
  gives one corrective action; non-interactive `use` never installs or
  prompts.
- [x] Reuse foundation platform-backend tests. Do not repeat Linux, WSL,
  macOS, native Windows, stale-version, and timeout permutations here.

### Implementation

- [x] On the first action requiring an absent supervisor, keep the dashboard
  visible and explain that one small per-user service maintains accounts and
  updates supported sessions.
- [x] Ask once using the interactive controller.
- [x] On approval, call the existing `DaemonManager` directly through a typed
  CLI service; do not shell out to the public Sidekick command.
- [x] If a compatible service is already installed but unavailable, attempt a
  bounded user-level restart and readiness check before offering reinstall.
  Preserve cached metrics and the dashboard throughout.
- [x] Stream sanitized installation, start, socket, provider, Codex broker,
  maintenance, and restart progress to the footer.
- [x] Do not detect, read, or retire an earlier Sidekick schedule here.
  Live guided setup on this machine runs only after Task 9 has completed the
  clean-break preinstall transition.
- [x] Continue the original activation automatically after readiness. Do not
  require another Enter.
- [x] On failure, preserve the dashboard and show one exact corrective action.
- [x] Persist only successful setup acknowledgement tied to the installed
  service protocol generation. Re-prompt after a real incompatible reinstall,
  not every launch.

### Verify and commit

- [x] Run the two guided-setup scenarios plus existing daemon lifecycle,
  interactive CLI, and output-safety regressions they touch.
- [x] Run Ruff, `ty`, and architecture checks, then commit.

## 8. Task 6 — Managed Migration Command, Warnings, and Doctor

**Commit:** `feat(cli): guide managed account migration`

### Tests first

- [x] Extend `tests/test_dashboard.py` with one migration command journey for
  `sidekick-usages migrate managed-auth`: secret-safe preview, resumable
  provider ordering, one-account failure continuation, setup-token
  preservation, independent Codex login, and final all-account proof.
- [x] Extend one existing doctor/render scenario with representative
  login-required and reconciliation warnings plus separate service,
  authority, native relation, metrics, queue, and journal health. Assert
  warnings are account-specific and not persistent selection badges.
- [x] Do not snapshot every warning sentence or create separate browser,
  cancellation, provider, and doctor-state matrices; provider plans already
  prove those transitions.

### Implementation

- [x] Add a resumable managed-auth migration coordinator that:
  1. validates the schema-version-three account index;
  2. ensures the service is installed and ready;
  3. migrates each Codex account independently;
  4. migrates each Claude account independently;
  5. preserves account-specific manual action without stopping later work;
  6. verifies all authorities and due state; and
  7. reports remaining actions without secrets.
- [x] Reject an earlier Sidekick layout rather than reading or converting it.
  Managed-auth migration changes only accounts recreated in the current
  clean-break schema.
- [x] Keep this command interactive when provider login is required. It may
  accept already-authorized continuation but never accepts tokens as command
  arguments.
- [x] Replace current generic token-expired copy with authority-specific
  actions. Do not display a persistent migration badge.
- [x] Ensure warnings do not displace cursor meaning and do not imply stale
  metrics are current.
- [x] Expand doctor through focused diagnostic modules rather than extending
  the current 756-line `doctor.py`.

### Verify and commit

- [x] Run the two migration/diagnostic scenarios plus existing provider and
  persistence migration and help regressions they touch.
- [x] Run Ruff and `ty`, inspect output for real identities and secrets, then
  commit.

## 9. Task 7 — Pseudoterminal, Performance, Packaging, and Platform Gates

**Commit:** `test(cli): verify interactive dashboard resilience`

### Pseudoterminal coverage

- [x] Add `tests/pty_support.py` using standard-library `pty`, selectors, and
  subprocess on Unix. Do not add `pexpect`.
- [x] In `tests/test_dashboard_pty.py`, add one main PTY journey covering
  first paint, representative movement, Tab, Enter, Esc, refresh, help,
  resize to narrow and back, and normal exit without duplicate full-dashboard
  output.
- [x] In the same file, add one forced-cleanup PTY journey that interrupts
  during a service event and proves Ctrl-C exit plus restored echo and
  canonical mode after a child or supervisor failure.
- [x] Do not add separate PTY tests for key aliases, no-color, every resize,
  every failure source, or behavior already proven in the pure controller.
- [ ] Run PTY integration on Linux and both macOS architectures in CI.
- [x] Test WSL service and rescue generation automatically; retain real WSL
  stop/start for Task 9.

### Performance coverage

- [x] Add `packaging/benchmark_dashboard.py` as a release measurement, not a
  pytest suite. Use one representative synthetic account count matching the
  current machine and one bounded larger snapshot.
- [x] Measure one fresh-process cached first paint with an explicit deadline
  and require no more than 250 ms. Do not create a repeated subprocess loop.
- [x] Measure cursor input to visible render in one bounded in-process trace
  and target p95 no more than 50 ms.
- [x] Permit exactly one short-lived global lookup-worker child per refresh,
  prove it is reaped before exit, and never fork per account. Record peak RSS
  from the same run; do not add a process-count or account-count performance
  matrix.
- [x] In that trace, prove the current six saved accounts are all submitted
  before awaiting a result, a fast completion is visible before a blocked
  account, and the final rows retain deterministic order.
- [x] Measure steady supervisor RSS, idle CPU, worker exit, and Codex callback
  isolation using the foundation gates.
- [x] Fail the architecture gate if prompt_toolkit enters supervisor,
  non-interactive, or help imports.

### Packaging and documentation

- [x] Update `pyproject.toml`, `uv.lock`, wheel smoke, exact distribution
  inspection, and generated Homebrew dependency resources for
  prompt_toolkit 3.0.52 and `wcwidth`.
- [ ] Update Linux, macOS, and Windows CI so required Unix platforms run PTY
  tests while native Windows proves feature-disabled behavior.
- [x] Update README command examples, keys, service setup, session coverage,
  unsupported modes, and uninstall behavior.
- [ ] Add synthetic before/after terminal captures to the completion record.

### Verify and commit

- [x] Run:

```bash
uv run pytest --cov=sidekick_usages
uv run ruff check src/ tests/ packaging/
uv run ty check src/ tests/ packaging/
uv run python packaging/check_architecture.py
uv run pre-commit run --all-files
npm ci
npm audit --audit-level=moderate
npm run lint:markdown
uv build
uv run python packaging/smoke_wheel.py --build
```

- [x] Inspect the wheel for all three entry points, prompt_toolkit, `wcwidth`,
  service templates, and absence of caches or credentials.
- [x] Record benchmark environment and exact results in the completion record.
- [ ] Commit the gate and documentation changes.

## 10. Task 8 — Release Acceptance Evidence Review

This is a verification-only gate. It creates no
`test_global_account_selection_acceptance.py`, repeats no full suite merely
for test count, and requires no empty commit.

- [x] Map all 24 design acceptance gates to the smallest existing focused
  test, static check, benchmark, packaging check, or authorized live rollout
  step.
- [x] Confirm security and recovery invariants have automated evidence while
  real provider-session and current-machine behaviors remain in Task 9.
- [x] Confirm no wrapper, alias, shell function, PATH shim, symlink
  replacement, or shell edit can be produced by the implementation.
- [x] If a critical gate has no evidence, add one focused assertion to the
  nearest existing task test. Do not add a parallel acceptance layer.
- [x] Run the complete local gate once from clean test state. Rerun only a
  failed or nondeterministic focused check, not the entire suite by default.

**Automated evidence reconciliation, 2026-07-26:** Tasks 1-6 and the focused
Task 7 artifact, PTY, performance, packaging, and architecture behavior are
implemented. The serialized local gate is green at `d669799`: 433 tests
passed, seven platform cases skipped, static and security checks passed, and
the exact wheel passed. The
[completion evidence](../completion/2026-07-23-interactive-global-account-selection.md)
maps all 24 gates without claiming cross-platform CI, provider-session checks,
current-machine migration, or live Claude selection. The macOS PTY and Windows
stream corrections are committed with focused proof; their final platform
matrix and release gates remain open.

## 11. Task 9 — Current-Machine Migration and Live Verification

**Commit:** `docs(completion): record managed account rollout`

This is the only task authorized to mutate the current machine's Sidekick or
provider state. Execute it only after Tasks 1-8 and all three earlier plans are
green.

**Read-only baseline, 2026-07-26:** This WSL installation has one idle root
Task Scheduler job named `sidekick-usages-refresh`; its last result is `1`.
The old Linux timer, service, marked cron block, current resident service, and
WSL rescue task are absent. Installed Sidekick 0.7.0 still owns
`daemon uninstall --backend task-scheduler`. Re-read every fact before
mutation; this baseline is evidence, not authorization.

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

- [ ] Build and smoke-test one exact wheel before changing the installed
  Sidekick tool or scheduler.
- [ ] Record the wheel path and digest. Treat the successful exact-wheel
  smoke, platform lifecycle tests, and release gates as the replacement
  artifact proof. Keep the old installation and its scheduler-removal command
  intact until that proof passes.
- [ ] Re-read every legacy scheduler backend. Require the owned legacy job to
  be idle, with no in-flight maintenance process. If any backend is
  unassessable or more than one is installed, stop.
- [ ] Use the still-installed old release to remove its exact observed
  backend. On this WSL machine the expected command is:

```bash
sidekick-usages daemon uninstall --backend task-scheduler
```

- [ ] Prove the legacy task, timer, service, LaunchAgent, and marked cron block
  are absent before installing the new service. If removal or read-back fails,
  stop with the old installation intact; do not start the new supervisor.
- [ ] Uninstall the old Sidekick tool, then install the already-proven exact
  wheel through the normal `uv tool` path:

```bash
uv tool uninstall sidekick-usages
uv tool install <absolute-verified-wheel>
```

- [ ] Do not install an old-layout reader, scheduler-retirement adapter,
  rollback writer, or other compatibility runtime. Recreate each account
  through supported Sidekick commands and official provider login.
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
- [ ] Prove the old periodic schedule is still absent and exactly one resident
  scheduler remains. On WSL, the one Windows rescue task may also remain; it
  starts systemd and performs no maintenance.
- [ ] If current-service installation or proof fails, leave the obsolete
  schedule absent, make no provider selection change, and repair the
  clean-break installation. Never restore the old scheduler beside the new
  one.
- [ ] On WSL, test Windows logon rescue, WSL stop/start, systemd recovery, and
  no duplicate maintenance.

### Migrate every account

- [ ] Run:

```bash
sidekick-usages migrate managed-auth
```

- [ ] For each Codex account, allocate the final private home, perform
  independent official login, verify identity and managed refresh, convert to
  sanitized metadata, retire any recreated current-schema stored authority
  only after success, and preserve current-schema metrics history.
- [ ] For each Claude account, allocate the final private profile, perform
  official subscription login, verify identity and protected storage,
  preserve any setup token, collect current metrics, and prove inactive
  maintenance.
- [ ] Obtain separate, just-in-time approval before any step that can change
  the live selected Claude account. Name the exact target; do not treat a
  rollout, migration, or provider-login approval as selection approval.
- [ ] Continue after account-scoped failures and return later. Ask the user
  only when the provider requires browser, MFA, password, or consent.
- [ ] Never manually copy or edit an account, credential file, private auth
  bundle, or Keychain item.

### Verify global selection

- [ ] Preserve the deliberate current native selections before testing.
- [ ] Restart only Codex TUIs that predate official daemon enrollment.
- [ ] With separate target-specific approval before each live Claude change,
  select the approved Claude account from the dashboard and verify a new bare
  `claude` uses it. Do not cycle Claude accounts while an active session must
  remain on its current account.
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
- [ ] All 24 approved acceptance gates have proportionate evidence:
  foundational security and recovery behavior is automated, while real
  provider-session and current-machine behavior is verified in Task 9.
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
| 21. Secret boundaries | Focused phase checks; Dashboard Task 8 |
| 22. Guided install and clean uninstall | Foundation Tasks 6-7; Dashboard Task 5 |
| 23. Current-machine migration | Dashboard Task 9 |
| 24. Earlier-layout rejection | Foundation Tasks 1 and 8 |
