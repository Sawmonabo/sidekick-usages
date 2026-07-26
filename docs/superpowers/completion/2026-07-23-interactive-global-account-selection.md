# Interactive global account selection completion evidence

## Status and scope

This record captures the completed automated implementation, focused
evidence, and serialized local gate for interactive global account selection
at commit `d669799`. It is not the final cross-platform or current-machine
rollout record.

The 24 approved acceptance gates all have a focused evidence owner. A gate
marked `Automated pass; live pending` has complete synthetic or static proof
for its automatable behavior, but still requires the explicitly reserved
Task 9 current-machine observation. `Live pending` means there is no safe
automated substitute for that claim.

Focused traceability is **24/24 mapped**. This does not mean that all 24 final
release gates have passed.

This record contains only synthetic account labels and secret-free
measurements. Its creation did not:

- access or mutate a real provider account;
- install, uninstall, or restart a Sidekick service;
- replace an installed Sidekick version or scheduler;
- change the selected Claude or Codex account;
- exercise a live provider session; or
- establish cross-platform CI success for the final commit.

Cross-platform fixes, current-machine migration, provider-session checks,
live WSL recovery, and target-specific Claude switch approval remain open in
the
[interactive rollout plan](../plans/2026-07-23-interactive-account-dashboard-and-rollout.md).

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
  complete serialized local gate.

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
| Installed-wheel cached first paint | 130.146 ms | 250 ms |
| Synthetic cached trace first paint | 103.586 ms | 250 ms |
| Six-account cursor-to-render p95 | 5.572 ms | 50 ms |
| Expanded cursor-to-render p95 | 16.465 ms | 50 ms |
| Reaped trace-process peak RSS | 46.805 MiB | 96 MiB |
| Reaped lookup-worker peak RSS | 46.250 MiB | 96 MiB |

The same trace proved:

- exactly one short-lived lookup-worker process was launched;
- one bounded seven-thread wave, below the eight-thread cap, started six
  saved-account tasks and one Claude-local activity task before awaiting a
  result;
- a fast account completed before the deliberately blocked account;
- final provider and account rows retained deterministic ordinal order; and
- both the lookup worker and trace process were reaped.

These numbers prove the focused Linux WSL2 artifact gate only. Cross-platform
CI is not green: the macOS Arm PTY failure and expected Windows portability
issue are under active correction. Live service measurements also remain
pending.

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

This local result does not replace the required platform matrix. The
confirmed macOS Arm PTY failure and expected Windows portability issue keep
cross-platform and final release gates open.

## Approved 24-gate evidence map

### 1. Vendor executable resolution

Evidence: exact executable provenance in the Claude and Codex provider
boundaries, plus the packaging command inventory.

Disposition: **Automated pass; live pending.** Task 9 must still record the
installed vendor paths and symlink targets before and after migration.

### 2. No shell or command interception

Evidence: the source-derived artifact contract, CLI architecture rules, and
the exact wheel entry-point inventory contain no `claude` or `codex` wrapper.

Disposition: **Automated pass; live pending.** Task 9 retains the real shell,
PATH, alias, function, and symlink comparison.

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

Disposition: **Automated pass; live pending.**

### 9. New ordinary terminals

Evidence: packaging proves that normal vendor commands remain vendor-owned,
and provider activation tests prove the selected native projection.

Disposition: **Live pending.** Task 9 must verify new bare vendor commands
after an authorized selection.

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

Disposition: **Automated pass; live pending.**

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

Disposition: **Cross-platform pending.** Platform-specific tests and workflow
definitions exist, but the macOS Arm PTY failure and expected Windows
portability issue must be corrected before the platform gate can pass. Live
WSL recovery also has not been recorded.

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

Disposition: **Automated pass; live pending.** Task 9 owns actual scheduler
retirement, installation, read-back, and uninstall verification.

### 23. Current-machine migration

Evidence owner: interactive rollout Task 9.

Disposition: **Live pending.** No migration, reinstall, provider login, or
selection was performed for this record.

### 24. Earlier-layout rejection

Evidence: the clean-break schema, migration coordinator, architecture rules,
and packaging contract reject compatibility readers and scheduler-retirement
runtime adapters.

Disposition: **Automated pass; live pending.** Task 9 must use the still
installed old release to remove only its observed scheduler before clean
installation.

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

These are sanitized test contracts, not captures of the current machine. The
final before/after terminal captures remain open for Task 9.

## Remaining release evidence

The feature is not yet at final release closure. The following evidence remains
open:

1. Correct the macOS Arm PTY failure and expected Windows portability issue,
   then record the final Linux, macOS Arm, macOS Intel, and Windows CI
   results.
2. Complete Task 9's read-only executable, account, scheduler, service, and
   recovery inventory.
3. Build and digest the exact rollout wheel, remove the observed legacy
   scheduler with the installed old release, and install the proven wheel.
4. Verify the resident user service, WSL rescue, restart recovery, queue,
   provider capability, broker, and maintenance state.
5. Migrate each account through Sidekick and official provider processes.
6. Obtain separate just-in-time approval before any live Claude selection.
7. Verify new and supported ongoing provider sessions without retargeting an
   in-flight request.
8. Record sanitized live before/after captures, exact remaining actions, and
   uninstall verification.
9. Close the four plan phase gates and final release gate only after the
    linked evidence is complete.
