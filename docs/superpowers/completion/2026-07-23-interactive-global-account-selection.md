# Interactive global account selection completion evidence

## Status and scope

This record captures the automated implementation and the authorized
current-machine rollout through `a2041cf`. Focused traceability remains
**24/24 mapped**. The implementation, clean v3 storage transition, exact-wheel
installation, resident WSL service, live read-only dashboard, and native
command-isolation checks are complete.

Final provider-auth rollout is intentionally incomplete:

- two Codex accounts still require independent official managed-home login;
- four Claude setup-token accounts still require official managed-profile
  association; and
- no Claude association or selection will run while an active Claude session
  must remain untouched without separate approval naming the exact target.

The current dashboard therefore presents the correct migration/login actions
and keeps account switching disabled. It does not claim that the six legacy
authorities are already managed or fresh.

This tracked record uses synthetic labels and secret-free measurements only.
It contains no provider IDs, credential paths, token hashes, raw provider
output, or account exports.

## Current-machine rollout checkpoint

| Boundary | Verified result |
| --- | --- |
| Storage | Six stable accounts migrated to strict v3: four Claude and two Codex |
| Final verified artifact | `sidekick_usages-0.7.0-py3-none-any.whl`, SHA-256 `5526f21d56106071e8871112a830385e89664049b282e99b17643fb9938f75b8` |
| Native CLIs | Claude Code `2.1.220`; Codex CLI `0.145.0` |
| Resident service | WSL user service active and enabled; peer, socket, protocol, process, platform, and rescue checks healthy |
| Scheduling | Legacy periodic task absent; one user supervisor plus one logon-only WSL rescue task |
| Maintenance | Four setup-token Claude rows scheduled independently; two legacy Codex rows parked once as `managed_auth_migration_required` |
| Live usage | Four Claude rows returned current metrics in one bounded lookup wave; both Codex rows returned exact managed-login actions |
| Interactive contract | One cursor started on the observed active Claude row; no healthy-row active badge; quit without an account action |
| Native isolation | Claude and Codex executables, ordinary commands, user wrapper, shell files, and native Codex login remained unchanged |
| Live Claude session | Existing session remained running and was not signaled, restarted, attached to, or retargeted |
| Remaining authority work | Two official Codex logins and four approved Claude associations |

The clean-break v2-to-v3 transition used the reviewed, local-only disposable
CLI at `86000b9`. It preserved stable account IDs and native provider files,
was never merged or pushed, and was removed with its worktree after the live
migration succeeded.

The live service removal and reinstall journey also passed. Removal made the
user service inactive and removed the WSL rescue task while preserving all six
accounts. Reinstallation restored one active, enabled supervisor and one
healthy rescue task. The account index and both native provider login files
were byte-for-byte unchanged across the cycle, and the pre-existing Claude
process remained running.

The exercised public command surfaces were:

```bash
uv run python packaging/smoke_wheel.py --build
uv tool install --force <verified-wheel>
sidekick-usages daemon uninstall
sidekick-usages daemon status
sidekick-usages daemon install
sidekick-usages doctor --json
sidekick-usages --no-interactive
sidekick-usages
claude --version
codex --version
```

The native Claude login changed once before final installation while the
pre-existing Claude session remained active. Its timestamp and eight-hour
expiry extension align with that official session refreshing its own login;
Sidekick's isolated probes did not select or log in an account. The native
file did not change during final installation, service verification, live
usage lookup, or the launch-and-quit dashboard check.

## Committed evidence

The final cached-first launcher and installed-artifact proof are anchored by:

- `2892392` — paints the passive cached dashboard before full CLI startup,
  preserves one-shot routing, and keeps the interactive process isolated;
- `61ed79e` — strengthens the two load-bearing PTY journeys for complete-frame
  detection, terminal restoration, and child cleanup;
- `1234807` — measures the exact installed `sidekick-usages` console script on
  a Unix PTY inside isolated application and provider paths;
- `d1d6ac2` — aligns the installed runtime dependency and benchmark contract;
  and
- `d669799` — applies the formatter-only dashboard startup cleanup after the
  complete serialized local gate;
- `fb8869a` — gives the private Windows Python child UTF-8 streams without
  changing the parent environment or public vendor commands; and
- `20d859e` — makes the existing PTY journeys wait for and validate one
  complete prompt-toolkit redraw under fragmented Unix PTY delivery;
- `b0b81c6` — parks legacy stored authorities once with an exact managed-auth
  migration action while preserving setup-token-only Claude maintenance;
- `3ef3108` — makes broker-owned WebSocket reads and writes cooperatively
  cancellable so Linux and macOS shutdown cannot wait on the activation
  deadline; and
- `c7f3cd5` — keeps the `250 ms` release gate on the exact installed public
  command while leaving the packaging-only first-paint trace as a measured
  diagnostic; and
- `b9cdb39` — measures the bounded in-process cursor-render benchmark as CPU
  cost, excluding hosted-runner descheduling without adding a retry, platform
  exception, or threshold increase; and
- `7d46332` — separates the fake Codex daemon's outer deadlock watchdog from
  the unchanged production recovery deadline so CI scheduling cannot mask the
  load-bearing failure; and
- `a2041cf` — binds the native macOS filesystem operation once, removes the
  unused Linux mount classifier from the macOS cold path, and makes any
  unchanged `250 ms` first-paint failure report its observed duration.

Supporting focused boundaries include:

- `tests/dashboard/test_state.py` for secret-free cached joins, stable IDs,
  provider read-back, external state, and stale metrics;
- `tests/dashboard/test_routing.py` for cached-first TTY routing and one-shot
  isolation;
- `tests/dashboard/test_actions.py` for guided setup, resumable migration, and
  non-prompting `use`;
- `tests/dashboard/test_pty.py` for the main interaction journey and forced
  cleanup;
- `tests/usage/test_render.py` for the representative wide and narrow cursor
  contracts;
- `tests/usage/test_service.py` and `tests/usage/test_activity.py` for bounded
  account lookup, completion-order independence, exact aggregation, and stale
  snapshot retention;
- the provider activation, recovery, maintenance, and broker suites for
  Claude and Codex; and
- the architecture and packaging gates for dependency direction, command
  ownership, import isolation, and exact wheel contents.

No additional acceptance test file or repeated platform matrix was added.

## Exact Linux WSL2 performance evidence

The release measurement installed one exact wheel into an isolated
environment, launched its public `sidekick-usages` console script on a Unix
PTY, and denied access to real provider commands and application paths.

| Measurement | Result | Required bound |
| --- | ---: | ---: |
| Installed-wheel cached first paint | 89.283 ms | 250 ms |
| Synthetic cached trace first paint | 93.750 ms | diagnostic |
| Six-account cursor-render CPU p95 | 4.790 ms | 50 ms |
| Expanded cursor-render CPU p95 | 14.315 ms | 50 ms |
| Reaped trace-process peak RSS | 46.629 MiB | 96 MiB |
| Reaped lookup-worker peak RSS | 46.117 MiB | 96 MiB |

The same trace proved:

- exactly one short-lived lookup-worker process was launched;
- one bounded seven-thread wave, below the eight-thread cap, started six
  saved-account tasks and one Claude-local activity task before awaiting a
  result;
- a fast account completed before the deliberately blocked account;
- final provider and account rows retained deterministic ordinal order; and
- both the lookup worker and trace process were reaped.

These numbers prove the final Linux WSL2 exact-artifact gate. The installed
public command remains a wall-clock `250 ms` product boundary. The synthetic
first-paint trace remains diagnostic, while the pure in-process cursor render
retains its unchanged `50 ms` CPU-cost gate without charging hosted-runner
descheduling to Rich rendering.

## Serialized local gate

The serialized local gate completed at `d669799` with:

- Ruff, Ty, and the repository architecture check green;
- one existing architecture cohesion warning for an 801-line test module;
- Bandit reporting zero findings and zero `nosec` suppressions;
- 433 tests passed, seven platform skips, and 440 collected cases in
  134.81 seconds;
- pre-commit green after the formatter-only `d669799` cleanup;
- npm audit reporting zero vulnerabilities and Markdown lint green; and
- `uv build` plus the exact-wheel smoke green.

This local result does not replace the required platform matrix. Focused
Linux proof passes for both corrections, but cross-platform and final release
gates remain open until the final matrix validates them.

## Approved 24-gate evidence map

### 1. Vendor executable resolution

Evidence: exact executable provenance in the Claude and Codex provider
boundaries, plus the packaging command inventory.

Disposition: **Automated and live pass.** Installed command resolution,
versions, and vendor targets were recorded before and after installation and
remained unchanged.

### 2. No shell or command interception

Evidence: the source-derived artifact contract, CLI architecture rules, and
the exact wheel entry-point inventory contain no `claude` or `codex` wrapper.

Disposition: **Automated and live pass.** Sidekick created no vendor wrapper,
alias, function, symlink, or PATH entry. The pre-existing user shell
customizations remained byte-for-byte unchanged.

### 3. Approved cursor interaction

Evidence: representative wide and narrow render tests, the pure controller
journey, and the main PTY journey.

Disposition: **Automated pass.**

### 4. One-shot paths do not block

Evidence: the routing test proves redirected I/O, `check`,
`--no-interactive`, help, version, supervisor, and worker paths do not enter
interactive input.

Disposition: **Automated pass.**

### 5. No healthy-row selection labels

Evidence: both representative render tests require exactly one cursor and
reject `IN USE`, `ACTIVATING`, `MIGRATION REQUIRED`, and `CURRENT`.

Disposition: **Automated pass.**

### 6. Account-specific warnings

Evidence: cached-state, render, migration, and doctor tests keep authority,
service, metric, reconciliation, and login warnings scoped to one account.

Disposition: **Automated pass.**

### 7. One-Enter healthy switch

Evidence: the controller and provider activation tests prove one action,
provider read-back, outgoing-authority retention, and verified commit.

Disposition: **Automated pass; live pending.** A live Claude switch requires
separate approval naming the exact target.

### 8. Independent provider selection

Evidence: selected-state, controller, Claude activation, and Codex activation
tests prove that changing one provider leaves the other provider unchanged.

Disposition: **Automated pass; provider login and live selection pending.**

### 9. New ordinary terminals

Evidence: packaging proves that normal vendor commands remain vendor-owned,
and provider activation tests prove the selected native projection.

Disposition: **Vendor ownership passed live; selected-account projection
pending.** Bare vendor commands still resolve normally. Account identity
cannot be verified until managed login and an authorized selection complete.

### 10. Supported ongoing sessions

Evidence: the Codex broker tests prove daemon-connected update and
rehydration behavior; Claude's documented boundary remains next-request
adoption.

Disposition: **Automated pass; live pending.** Exact installed-session
behavior remains a Task 9 observation.

### 11. In-flight stability

Evidence: activation, broker, queue, and interruption tests separate committed
selection from already-running operations.

Disposition: **Automated pass; live pending.** Task 9 must observe an installed
provider request without retargeting it.

### 12. Unselected maintenance and metrics

Evidence: the Claude and Codex maintenance tests, global lookup wave, and
activity snapshot tests prove selection-independent work and failure
isolation.

Disposition: **Automated pass; managed-authority live proof pending.** All
four setup-token Claude accounts returned fresh usage independently. Both
legacy Codex accounts were isolated and returned the correct managed-login
action instead of a false refresh-token diagnosis.

### 13. Fixed setup-token tracking

Evidence: Claude migration, maintenance, lifetime, usage, and heartbeat tests
preserve setup-token authority and never treat it as refreshable.

Disposition: **Automated pass.**

### 14. Independent Codex repair

Evidence: managed Codex login and refresh tests use one final private home per
account and continue after an account-scoped failure.

Disposition: **Automated pass.**

### 15. Per-account failure isolation

Evidence: the bounded usage wave, provider maintenance, queue, and supervisor
tests prove that one blocked, rejected, or failed account does not stop later
work.

Disposition: **Automated pass.**

### 16. External login reconciliation

Evidence: the Claude recovery and Codex activation tests let known and unknown
official external state win without silent import.

Disposition: **Automated pass; live pending.**

### 17. Interrupt recovery

Evidence: Claude rollback, Codex activation recovery, persistence journals,
supervisor crash isolation, and the forced-cleanup PTY journey.

Disposition: **Automated pass.**

### 18. Supervisor performance

Evidence: the foundation supervisor gates and the exact dashboard benchmark
prove bounded memory, callback isolation, one lookup worker, a bounded thread
wave, deterministic output, and process reaping.

Disposition: **Automated pass.**

### 19. Required platform coverage

Evidence: platform-specific lifecycle artifacts, native Windows
feature-disabled behavior, WSL rescue generation, and CI matrix definitions
exist for Linux, macOS Arm, macOS Intel, and Windows.

Disposition: **Final matrix pending.** The shutdown regression is green on
Linux, macOS Arm, and macOS Intel; Windows behavior remains feature-disabled
as designed. The final `a2041cf` workflow run must finish before this gate can
close. Installed WSL service and logon-rescue health passed; destructive WSL
termination remains deferred.

### 20. Pre-mutation capability failure

Evidence: Claude and Codex provider-boundary tests reject unsupported
executables, schemas, profiles, storage, and native Windows before mutation.

Disposition: **Automated pass.**

### 21. Secret boundaries

Evidence: account-index, persistence, process, control-protocol, architecture,
output-safety, benchmark-isolation, and exact artifact tests use synthetic
identities and reject secret-bearing representations.

Disposition: **Automated pass.**

### 22. Guided install and clean uninstall

Evidence: daemon lifecycle tests and the guided-setup journey prove
user-scoped artifacts, one confirmation, readiness verification, action
resumption, refusal, failure, and owned cleanup.

Disposition: **Automated and live pass.** The legacy periodic task is absent;
clean removal deleted only the Sidekick service and rescue task; reinstall
restored one active, enabled user supervisor and one healthy WSL rescue
without changing accounts or native provider state.

### 23. Current-machine migration

Evidence owner: interactive rollout Task 9.

Disposition: **Storage and installation passed live; managed authentication
pending.** Six accounts were migrated through the disposable CLI into strict
v3 state and the exact wheel was installed. Two Codex logins and four Claude
associations remain.

### 24. Earlier-layout rejection

Evidence: the clean-break schema, migration coordinator, architecture rules,
and packaging contract reject compatibility readers and scheduler-retirement
runtime adapters.

Disposition: **Automated and live pass.** The old periodic scheduler is
absent, the strict v3 store is current, and no compatibility reader,
retirement adapter, or rollback writer was installed.

## Synthetic before-and-after captures

These captures were generated from the current renderers at 120 and 52
columns with synthetic account models. `Before` is the preserved one-shot
`check` presentation; `after` is the interactive default presentation. It is
a product-path comparison, not a claim that the one-shot command was removed.

### Wide before: one-shot check

```text
      o
     .-.
  .--┴-┴--.    sidekick usages
  | O   O |   >> A multi-account usage dashboard for Claude Code and Codex CLI.
  | ||||| |   >> Limits + resets + account status, one terminal.
  '--___--'
───────────────────────────────────────────────────────────────────────────────────

╭─ CLAUDE · 2 accounts ───────────────────────────────────────────────────────────╮
│                                                                                 │
│                                    5h      7d                                   │
│  ●  work@example.test      max      0%     51%                                  │
│                                  3h 50m  3h 50m                                 │
│  ⚠ work@example.test: last known · 2026-06-12T10:20:56.789000+00:00             │
│                                                                                 │
│  ●  personal@example.test  max   ⚠ authentication failed                        │
│                                  Claude rejected the saved subscription login.  │
│                                  Sign in to that Claude account, then run:      │
│                                  sidekick-usages refresh personal@example.test  │
│                                                                                 │
╰───────────────────────────────────── 903,464,085 tokens  ·  since Dec 28, 2025 ─╯

╭─ CODEX · 1 account ─────────────────────────────────────────────────────────────╮
│                                                                                 │
│                                    5h      7d                                   │
│  ●  codex@example.test     pro      8%     45%                                  │
│                                  3h 50m  3h 50m                                 │
│                                                                                 │
╰──────────────────────────────────── 7,449,473,297 tokens  ·  since Apr 7, 2026 ─╯

 <40    40-69    70-89    ≥90      dim = resets in
```

### Wide after: interactive default

```text
      o
     .-.
  .--┴-┴--.    sidekick usages
  | O   O |   >> A multi-account usage dashboard for Claude Code and Codex CLI.
  | ||||| |   >> Limits + resets + account status, one terminal.
  '--___--'
─────────────────────────────────────────────────────────────────────────────────────────────────

╭─ CLAUDE · 2 accounts ─────────────────────────────────────────────────────────────────────────╮
│                                                                                               │
│                                         5h      7d                                            │
│  › ●  work@example.test         max      0%     51%                                           │
│                                       3h 50m  3h 50m                                          │
│                                                                                               │
│    ●  personal@example.test     max                                                           │
│  ⚠ work@example.test: Metrics last updated 2h 14m ago; retry scheduled.                       │
│  ⚠ personal@example.test: Complete the official Claude Code login before using this account.  │
│                                                                                               │
╰─────────────────────────────────────────────────── 903,464,085 tokens  ·  since Dec 28, 2025 ─╯

╭─ CODEX · 2 accounts ──────────────────────────────────────────────────────────────────────────╮
│                                                                                               │
│                                         5h      7d                                            │
│    ●  codex@example.test        pro      8%     45%                                           │
│                                       3h 50m  3h 50m                                          │
│                                                                                               │
│    ●  External Codex CLI login                                                                │
│  ⚠ External Codex CLI login: This external login is not saved in Sidekick.                    │
│                                                                                               │
╰────────────────────────────────────────────────── 7,449,473,297 tokens  ·  since Apr 7, 2026 ─╯

 <40    40-69    70-89    ≥90      dim = resets in

 Switching to personal@example.test… verifying with Claude Code
```

### Narrow before: one-shot check

```text
      o
     .-.
  .--┴-┴--.  sidekick usages
  | O   O |
  | ||||| |
  '--___--'
────────────────────────────────────────────────────

work@example.test  [claude · max]
  Last known · 2026-06-12T10:20:56.789000+00:00
  ⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …
  ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …

codex@example.test  [codex · pro]
  ⣿⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …
  ⣿⣿⣿⣿⣿⣿⣿⣿⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …

personal@example.test  [claude · max]
  ⚠ authentication failed
  Claude rejected the saved subscription login.
  Sign in to that Claude account, then run:
  sidekick-usages refresh personal@example.test

CLAUDE · 903.46M tokens
         since Dec 28, 2025

CODEX · 7.449B tokens
        since Apr 7, 2026
```

### Narrow after: interactive default

```text
      o
     .-.
  .--┴-┴--.  sidekick usages
  | O   O |
  | ||||| |
  '--___--'
────────────────────────────────────────────────────

› ● work@example.test  [claude · max]
  ⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …
  ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …
    ⚠ Metrics last updated 2h 14m ago; retry
    scheduled.

  ● personal@example.test  [claude · max]
    ⚠ Complete the official Claude Code login before
    using this account.

CLAUDE · 903.46M tokens
         since Dec 28, 2025

  ● codex@example.test  [codex · pro]
  ⣿⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …
  ⣿⣿⣿⣿⣿⣿⣿⣿⣀⣀⣀⣀⣀⣀…    ↻ Fri Jun 12, 12:24 PM (in 3h …

  ● External Codex CLI login  [codex]
    ⚠ This external login is not saved in Sidekick.

CODEX · 7.449B tokens
        since Apr 7, 2026

 Switching to personal@example.test… verifying with
Claude Code
```

Manual inspection confirms that both widths retain the recognizable robot,
provider grouping, metric meaning, reset timing, and account-specific
warnings. The interactive path adds one cursor and one bounded action footer;
it adds no healthy-row active-state label.

## Synthetic dashboard contract

The wide and narrow automated render scenarios use only labels such as
`work@example.test`, `personal@example.test`, and `codex@example.test`. They
prove:

- the robot masthead and recognizable provider layout remain;
- exactly one `›` cursor precedes the existing account bullet;
- healthy rows have no active-state badge;
- actionable and stale detail stays account-specific;
- the key or progress footer is bounded; and
- wide and narrow output preserve activity totals and reset meaning.

These are sanitized product contracts. The live launch matched the
cursor-only layout and was closed with `q` before any account action.

## Remaining release evidence

The implementation and authorized machine transition are complete. Final
release closure still requires:

1. a green terminal result for the final `a2041cf` cross-platform workflow;
2. independent official login for both saved Codex accounts;
3. official association of each Claude setup-token account, with separate
   target-specific approval before any step that can alter the live Claude
   selection;
4. post-login verification of new and supported ongoing provider sessions,
   in-flight stability, and cross-provider independence; and
5. the deferred destructive WSL terminate/recovery journey, which cannot run
   while the active Claude session must remain alive.
