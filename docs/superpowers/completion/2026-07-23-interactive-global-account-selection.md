# Interactive global account selection completion evidence

## Status and scope

This record captures the automated implementation and the authorized
current-machine rollout through `902621e`. Focused traceability remains
**24/24 mapped**. The implementation, clean v3 storage transition, exact-wheel
installation, resident WSL service, live read-only dashboard, and native
command-isolation checks are complete. Both saved Codex accounts now have
independent managed authority, selection works through the installed CLI, and
the resident service starts and recovers the official shared Codex runtime
without manual intervention.

Final Claude provider-auth rollout is intentionally deferred:

- four Claude setup-token accounts still require official managed-profile
  association; and
- no Claude association or selection will run while an active Claude session
  must remain untouched without separate approval naming the exact target.

The current dashboard presents live Claude and Codex metrics, padded usage
tiles, account-local warnings, cursor-only selection, and both provider token
totals. Claude setup-token rows truthfully retain their official-login action;
Codex rows are managed, maintained, and free of false service or update
warnings.

This tracked record uses synthetic labels and secret-free measurements only.
It contains no provider IDs, credential paths, token hashes, raw provider
output, or account exports.

## Current-machine rollout checkpoint

| Boundary | Verified result |
| --- | --- |
| Storage | Six stable accounts migrated to strict v3: four Claude and two Codex |
| Final verified artifact | `sidekick_usages-0.7.0-py3-none-any.whl`, SHA-256 `0d14f8c04b232b6f9c16d4e5da10c384be5312632288c12b4738ecd998f451ad` |
| Native CLIs | Claude Code `2.1.220`; Codex CLI `0.145.0` |
| Resident service | WSL user service active and enabled; peer, socket, protocol, process, platform, and rescue checks healthy |
| Scheduling | Legacy periodic task absent; one user supervisor plus one logon-only WSL rescue task |
| Maintenance | Four setup-token Claude rows scheduled independently; both managed Codex authorities healthy and independently scheduled with zero failed attempts |
| Live usage | All six rows returned current metrics in one bounded lookup wave; Claude and Codex token totals rendered |
| Interactive contract | One cursor identified the selected row; padded usage tiles and account-local warnings rendered; no active badge; quit without an unintended account action |
| Native isolation | Vendor executables, ordinary commands, user wrapper, shell files, and the active Claude login remained untouched; Sidekick projected only the selected Codex authority |
| Live Claude session | Existing session remained running and was not signaled, restarted, attached to, or retargeted |
| Remaining authority work | Four approved Claude associations, intentionally deferred |

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
uv run python packaging/smoke_wheel.py --build --output-dir <fresh-output>
uv tool install --force --reinstall <verified-wheel>
sidekick-usages daemon uninstall
sidekick-usages daemon status
sidekick-usages daemon install
sidekick-usages doctor --json
sidekick-usages --no-interactive
sidekick-usages
sidekick-usages use codex <saved-account>
claude --version
codex --version
codex login status
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
- `d669799` — applies the formatter-only dashboard startup cleanup after the
  complete serialized local gate;
- `fb8869a` — gives the private Windows Python child UTF-8 streams without
  changing the parent environment or public vendor commands;
- `20d859e` — makes the existing PTY journeys wait for and validate one
  complete prompt-toolkit redraw under fragmented Unix PTY delivery;
- `b0b81c6` — parks legacy stored authorities once with an exact managed-auth
  migration action while preserving setup-token-only Claude maintenance;
- `3ef3108` — makes broker-owned WebSocket reads and writes cooperatively
  cancellable so Linux and macOS shutdown cannot wait on the activation
  deadline;
- `c7f3cd5` — keeps the `250 ms` release gate on the exact installed public
  command while leaving the packaging-only first-paint trace as a measured
  diagnostic;
- `b9cdb39` — measures the bounded in-process cursor-render benchmark as CPU
  cost, excluding hosted-runner descheduling without adding a retry, platform
  exception, or threshold increase;
- `7d46332` — separates the fake Codex daemon's outer deadlock watchdog from
  the unchanged production recovery deadline so CI scheduling cannot mask the
  load-bearing failure;
- `a2041cf` — binds the native macOS filesystem operation once, removes the
  unused Linux mount classifier from the macOS cold path, and makes any
  unchanged `250 ms` first-paint failure report its observed duration;
- `014f1cf` — replaces Rich on the startup and interactive render path with
  one bounded, Unicode-aware renderer while retaining Rich for one-shot
  presentation;
- `cb437dd` — scopes resident-service readiness by provider and makes
  unmanaged accounts display the official login requirement without hiding
  managed Codex broker recovery;
- `d7d88be` — gives the managed-Codex broker requirement one lightweight
  policy owner shared by lifecycle health and the cached dashboard; and
- `4b39d2d` — replaces a timing poll in the synthetic Codex interruption
  proof with the supervisor-shutdown completion barrier that owns the durable
  retry transition;
- `30f3044` — resolves managed credentials through one shared composition
  owner in both one-shot and concurrent dashboard lookups;
- `7c9122e` — pins the qualified Codex executable in Linux, WSL, and macOS
  user-service artifacts without changing terminal command resolution;
- `455a800` — restores padded usage tiles, account-local warnings, Claude
  activity totals, and the strict provider-scoped activity contract;
- `c48d61e` — exposes typed broker failures and rejects unavailable Codex
  activation before queue insertion;
- `8d7a991` — aligns the exact-wheel synthetic activity fixture with the
  strict activity schema;
- `703afb2` — attaches Sidekick to the official shared Codex runtime;
- `8cd0205` — isolates provider operation slots so unrelated account work
  remains concurrent;
- `90a738a` — waits for recovered broker readiness after a resident-service
  restart;
- `cc14120` — propagates the service-qualified Codex executable into isolated
  workers and persists exact worker preparation failures;
- `d0d541d` — honors the official native Codex file-store default while
  retaining strict managed-profile configuration; and
- `902621e` — recognizes the official projected ChatGPT-token auth mode during
  post-activation reconciliation.

Supporting focused boundaries include:

- `tests/dashboard/test_state.py` for secret-free cached joins, stable IDs,
  provider read-back, external state, stale metrics, and the managed versus
  unmanaged Codex broker boundary;
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
| Installed-wheel cached first paint | 81.740 ms | 250 ms |
| Synthetic cached trace first paint | 91.385 ms | diagnostic |
| Six-account cursor-render CPU p95 | 2.082 ms | 50 ms |
| Expanded cursor-render CPU p95 | 5.753 ms | 50 ms |
| Reaped trace-process peak RSS | 46.398 MiB | 96 MiB |
| Reaped lookup-worker peak RSS | 46.398 MiB | 96 MiB |

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
descheduling to dashboard rendering.

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
Linux proof at `d7d88be` added 35 passing dashboard, lifecycle, routing, and
render cases plus green Ruff, Ty, architecture, Bandit, and pre-commit gates.
At `4b39d2d`, the one affected synthetic Codex interruption journey, Ruff, Ty,
architecture, and pre-commit gates also passed. The exact installed artifact
then passed the isolated startup, concurrent lookup, memory,
deterministic-order, and process-reaping benchmark above.

The final Codex worker and reconciliation repairs were verified through only
the two existing load-bearing activation and durable-worker boundaries. No
new test file or broad suite was added or rerun. Focused Ruff, Ty,
architecture, wheel-smoke, and live installed-CLI checks passed; architecture
reported only its existing non-failing cohesion warnings.

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

Disposition: **Automated pass; Codex live pass.** Installed Codex selection
completed with provider read-back. A live Claude switch remains intentionally
deferred.

### 8. Independent provider selection

Evidence: selected-state, controller, Claude activation, and Codex activation
tests prove that changing one provider leaves the other provider unchanged.

Disposition: **Automated and Codex live pass.** Selecting the saved Codex
authority left Claude state and the active Claude process untouched. Claude
selection remains intentionally deferred.

### 9. New ordinary terminals

Evidence: packaging proves that normal vendor commands remain vendor-owned,
and provider activation tests prove the selected native projection.

Disposition: **Codex live pass; Claude selection deferred.** Bare vendor
commands still resolve normally, and a new ordinary Codex invocation reads
the selected native projection without a wrapper, alias, or global
environment change.

### 10. Supported ongoing sessions

Evidence: the Codex broker tests prove daemon-connected update and
rehydration behavior; Claude's documented boundary remains next-request
adoption.

Disposition: **Automated and Codex live pass.** The resident Codex runtime
accepted the selected-authority update without terminating the current Codex
session. Claude next-request adoption remains intentionally deferred.

### 11. In-flight stability

Evidence: activation, broker, queue, and interruption tests separate committed
selection from already-running operations.

Disposition: **Automated and Codex live pass.** Same-account Codex projection
completed without interrupting the active Codex session. The active Claude
session was not signaled or retargeted.

### 12. Unselected maintenance and metrics

Evidence: the Claude and Codex maintenance tests, global lookup wave, and
activity snapshot tests prove selection-independent work and failure
isolation.

Disposition: **Automated and Codex live pass.** All four setup-token Claude
accounts returned fresh usage independently. Both Codex authorities are
managed and independently scheduled with healthy refresh state; selection of
one does not suspend maintenance or metrics for the other.

### 13. Fixed setup-token tracking

Evidence: Claude migration, maintenance, lifetime, usage, and heartbeat tests
preserve setup-token authority and never treat it as refreshable.

Disposition: **Automated and live pass.** All four fixed setup-token
authorities remain distinct, preserved, independently scheduled, and usable
for metrics without being misrepresented as refreshable subscriptions.

### 14. Independent Codex repair

Evidence: managed Codex login and refresh tests use one final private home per
account and continue after an account-scoped failure.

Disposition: **Automated and live pass.** Both managed Codex authorities are
healthy and independently scheduled; a same-account projection completed
without overwriting the other authority.

### 15. Per-account failure isolation

Evidence: the bounded usage wave, provider maintenance, queue, and supervisor
tests prove that one blocked, rejected, or failed account does not stop later
work.

Disposition: **Automated pass.**

### 16. External login reconciliation

Evidence: the Claude recovery and Codex activation tests let known and unknown
official external state win without silent import.

Disposition: **Automated and Codex live pass.** Reconciliation recognized the
official projected ChatGPT-token auth mode and retained the verified selected
authority. Claude reconciliation remains intentionally deferred.

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

Disposition: **Automated pass; destructive WSL recovery deferred.**
[CI run 30215717164](https://github.com/Sawmonabo/sidekick-usages/actions/runs/30215717164)
completed green at `4b39d2d`:

- Linux: 433 passed, seven skipped, `126.299 ms` installed first paint,
  `46.281 MiB` peak RSS;
- macOS Arm: 432 passed, eight skipped, `88.446 ms` installed first paint,
  `46.906 MiB` peak RSS;
- macOS Intel: 432 passed, eight skipped, `236.508 ms` installed first paint,
  `42.648 MiB` peak RSS;
- Windows: 387 passed, 51 skipped, with native account switching truthfully
  feature-disabled; and
- both Homebrew source builds, pre-commit, and the exact wheel and source
  distribution build passed.

Installed WSL service and logon-rescue health also passed. Destructive WSL
termination remains deferred while the active Claude session must remain
alive.

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

Disposition: **Storage, installation, and Codex authentication passed live.**
Six accounts are in strict v3 state, the exact wheel is installed, and both
Codex authorities are managed and maintained. Four Claude official
associations remain intentionally deferred.

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

The implementation and authorized machine transition are complete through
Codex. Final release closure still requires:

1. official association of each Claude setup-token account, with separate
   target-specific approval before any step that can alter the live Claude
   selection;
2. post-association verification of new and supported ongoing Claude
   sessions; and
3. the deferred destructive WSL terminate/recovery journey, which cannot run
   while the active Claude session must remain alive.
