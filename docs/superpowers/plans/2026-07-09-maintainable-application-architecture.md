# Maintainable Application Architecture Implementation Plan

> **For agentic workers:** Execute this plan one task at a time. Use
> `superpowers:subagent-driven-development` or
> `superpowers:executing-plans` when that capability is available. Preserve
> the stop/go gates, review boundaries, and verification requirements even
> when a different execution mechanism is used.

- **Status:** Approved
- **Date:** 2026-07-09
- **Operator approval date:** 2026-07-09
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Audited branch:** `develop`
- **Audited commit:** `42cd01eb17c7903b385b1b4e259cf5b0c64126c5`
- **Execution base:**
  `73ce06891747a0571276b35c3f54c7de2c4e188f`
- **Design authority:**
  `docs/superpowers/specs/`
  `2026-07-09-maintainable-application-architecture-design.md`
- **Initial approved design SHA-256 at plan publication:**
  `42553a0cb6ce24a6fadb8f166fb1d7e9cd3bd16a029e40f5f8de365baaeb166e`

## 1. Outcome

Transform Sidekick Usages from a working but CLI-heavy package into the
approved modular application without changing its product identity or losing
existing user data.

The completed implementation has:

- a narrow, infrastructure-independent `core/` package;
- typed provider, persistence, HTTP, credential, usage, and heartbeat
  boundaries;
- a registration-only CLI application with lazily composed runtime services;
- provider-owned Claude and Codex packages;
- validated and versioned persistence with explicit recovery behavior;
- pooled HTTP transport with exactly one retry owner;
- centralized Sidekick-owned path discovery and a safe, conditional native
  path migration;
- aware UTC application time and separate monotonic HTTP deadlines;
- stable human, JSON, quiet, scheduled, and version output contracts;
- meaningful behavior tests and mechanical architecture gates; and
- no production module over 1000 lines.

This is one integrated architecture migration. The phases exist to make it
safe and reviewable, not to defer required work.

## 2. Source-of-truth and execution contract

The approved design is the source of intent. Do not reopen an approved design
decision merely because the current implementation lacks the supporting
boundary. Change the implementation to support the approved result.

Reopen a design decision only when refreshed source evidence proves one of
these conditions:

- the approved behavior cannot be implemented safely;
- a dependency no longer supports Python 3.14 or a supported platform;
- a security, licensing, packaging, or maintenance fact materially changed;
- the live code moved enough that the planned ownership is no longer valid;
  or
- two approved requirements are irreconcilable.

When that happens, stop the affected task, record the evidence in a tracked
document, update the design decision, and obtain approval before continuing.
Do not choose a silent fallback.

### 2.1 Repository and change discipline

Before each task:

- confirm the repository, branch, base commit, and upstream relationship;
- inspect the entire worktree and preserve unrelated user changes;
- refresh exact callers, imports, line counts, dependency versions, and tests;
- search the relevant package for the exact concept name before adding one;
- read at least two neighboring implementation files and their tests;
- identify the smallest production-valid review boundary;
- write or retain only tests that protect observable behavior;
- run the focused gate before the full gate; and
- inspect the final diff for drift, secrets, suppressions, and dead code.

Never commit directly to `main`. Use Conventional Commits. Commit and push
only when the operator requests those actions.

Use `apply_patch` for hand-authored edits. Formatting and generated lockfile
updates may use their owning tools.

### 2.2 Research and dependency discipline

Every consequential dependency decision must be refreshed from current
primary sources immediately before adoption. Record:

- canonical owner and repository;
- current stable version and retrieval date;
- Python 3.14 and supported-platform compatibility;
- API fit against the real Sidekick call shapes;
- transitive dependencies, license, provenance, and security posture;
- release cadence, maintainer depth, and issue responsiveness;
- startup, runtime, wheel, Homebrew, and test impact;
- owned-code comparison and two-to-three-year maintenance cost; and
- rejection reasons and reversal conditions.

Inline the final decision in the design authority. If the supporting evidence
is too extensive, use the tracked research layout defined by the design. No
ignored or local-only artifact may become a normative reference.

Do not add a runtime dependency before its decision gate passes. Do not add
`pydantic-settings`; there is no approved settings contract.

### 2.3 Production-valid phase rule

Every committed task must leave the package installable and the documented
public behavior valid. A local intermediate edit may temporarily break
imports, but no commit may contain:

- both a same-named module and replacement package;
- a core model that stores provider-native expiry units;
- two persistence or retry owners;
- a compatibility shim with no supported external consumer;
- a migration that can silently discard or overwrite data; or
- a partially moved command tree.

Atomic module-to-package conversions may therefore be larger than an ordinary
edit. Their tests and package-content checks must land in the same commit.

## 3. Audited baseline

Refresh this section if implementation starts from a different commit.

### 3.1 Live quality baseline

At the audited commit:

- Python is 3.14.4;
- the package contains 28 production modules and 8454 production lines;
- tests contain 21 modules, 4646 lines, and 182 source test functions that
  expand to 190 collected cases;
- `uv run pytest` passes all 190 collected tests;
- `uv run ruff check src/ tests/` passes;
- `uv run ty check src/ tests/` passes;
- the focused Ruff line-length run reports 15 `E501` findings;
- production and tests still contain `Any`, `cast(...)`, and ten `# noqa`
  suppressions;
- only two production modules use the legacy future-annotations behavior;
- the repository-wide Markdown gate reports 93 existing errors in the June
  TUI plan and design; and
- the new architecture design passes targeted Markdown lint.

The 93 Markdown findings are baseline debt, not permission to keep the final
gate red. CS-05 resolves them without changing the approved TUI behavior.
Until CS-05 lands, run the repository-wide Markdown command and prove that no
new finding was introduced beyond this recorded baseline. CS-05 must reduce
the count to zero; every later change set requires a green Markdown gate.

The CS-01 execution refresh also proved that Python 3.14's native deferred
annotations supersede the repository's future-import instruction. The
configured `pyupgrade --py314-plus` hook removed all five imports present at
the execution base. The design now records the current Python documentation,
`AGENTS.md` requires the native behavior, and CS-06 verifies that the legacy
stringizing import cannot return.

The first [publication CI run][publication-ci] supplied two additional
execution-base facts under Python 3.14.6 on Linux, macOS, and Windows:

- pre-commit rewrote the same five legacy future imports and therefore failed
  its clean-tree gate; and
- three help-ordering cases failed because CI-enabled Rich styling inserted
  ANSI sequences inside the expected usage line, not because semantic help
  content or ordering changed.

The current worktree removes the five imports and makes the existing help test
exercise CI mode explicitly, normalize styling with Click, and retain the same
one-header and ordering assertions. The isolated Python 3.14.6 reproduction
passes. These gate repairs change no application behavior and should remain
separate review commits from the daemon safety fix when publication is
authorized.

The first [CS-01 publication run][cs01-publication-ci] then passed Linux,
macOS, and pre-commit but exposed two Windows-only failures in existing
failure-panel tests. Those cases constructed `Console` directly and bypassed
the test module's established `legacy_windows=False` renderer, so Rich changed
rounded corners to its safe square Windows form. The product title, counts,
failure content, and layout remained correct. Both cases now reuse the existing
cross-platform render helper while retaining their strict panel, count,
failure-content, and no-orphan-header assertions.

### 3.2 Current dependency and packaging baseline

The current direct runtime dependencies are:

- Click;
- Typer; and
- Rich.

`platformdirs` appears only as an indirect development-lock dependency. It is
not yet an approved direct runtime dependency. No schema-validation, pooled
HTTP, or independent retry dependency is currently declared.

The installed entry point is:

```toml
sidekick-usages = "sidekick_usages.cli:app"
```

The wheel uses the complete `src/sidekick_usages` package. Module-to-package
conversions must therefore inspect the built artifact for stale files, not
only the source tree.

### 3.3 Current structural baseline

The largest production modules are:

- `cli.py`: 2274 lines;
- `render.py`: 739 lines;
- `daemon.py`: 668 lines;
- `providers/codex.py`: 587 lines;
- `providers/claude.py`: 501 lines; and
- `heartbeat/service.py`: 472 lines.

The current root command surface includes:

- default invocation and `check`;
- `add`, `list`, `remove`, `rename`, `set-plan`, `refresh`, and `reset`;
- `heartbeat` and `daemon` command groups;
- `maintain` and `doctor`;
- `codex-login`, `codex-export`, and `setup-token`;
- `check-update` and `update`; and
- `--only`, `--version`, and `--help` root options.

Every command remains available through its approved final command or
time-bounded compatibility alias.

## 4. Target ownership map

The final package structure is defined by the design. This file-move map makes
the implementation sequence explicit.

| Current owner | Final owner |
|---|---|
| `store.py:Account` | `core/models.py` |
| `providers/base.py:DetectedCredentials` | `core/models.py` |
| `report.py` models | `core/models.py` |
| provider-neutral identifiers/statuses | `core/types.py` |
| repeated expiry classification | `core/expiry.py` |
| recursive JSON vocabulary | `serialization/json.py` |
| `store.py:AccountStore` | `persistence/account_store.py` |
| persisted record parsing | `persistence/schemas.py` |
| schema and location migration | `persistence/migrations.py` |
| `http.py` | `http/client.py` and `http/retry.py` |
| Sidekick-owned path reconstruction | `paths.py` |
| application wall-time acquisition | `clock.py` |
| CLI usage orchestration | `usage/service.py` |
| usage result objects | `usage/models.py` |
| `render.py` usage presentation | `usage/render.py` |
| CLI credential orchestration | `credentials/service.py` |
| `heartbeat/domain.py` | `heartbeat/models.py` |
| `heartbeat/base.py` | `heartbeat/ports.py` |
| concrete heartbeat adapters | provider packages |
| `providers/claude.py` | `providers/claude/` |
| `providers/codex.py` | `providers/codex/` |
| explicit provider construction | `providers/registry.py` |
| `cli_help.py` | `cli/help.py` |
| `token_input.py` | `cli/token_input.py` |
| command functions in `cli.py` | `cli/commands/` |
| runtime composition in `cli.py` | `cli/app.py` and `cli/context.py` |

Top-level `maintenance.py`, `doctor.py`, `daemon.py`, `lifetime.py`, and
`update.py` remain cohesive feature modules unless implementation evidence
shows that one has crossed the size limit or gained a distinct responsibility.
Do not create empty symmetry packages.

## 5. Dependency graph and release slices

```text
safety fixes and baseline hygiene
        |
        v
schema validation ---- HTTP decision ---- native path decision
        |                    |                    |
        +--------------------+--------------------+
                             |
                             v
wall clock -> HTTP package -> current paths -> core -> typed persistence
                                                       |
                                                       v
usage service -> heartbeat ports -> provider packages
                                      |
                                      v
typed provider contracts -> credential service -> CLI package
                                                  |
                                                  v
                         conditional native path migration
                                                  |
                                                  v
provider commands -> presentation -> mechanical gates -> parity proof
```

Native path relocation is deliberately scheduled after the Codex provider
package and final CLI composition root exist. This avoids temporary auth and
composition wiring that would move immediately. On a recorded GO, the
conditional change defines the `PrivateAuthMigrator` port, implements it in
Codex auth, and injects it from final composition. A NO-GO creates none of
those migration-only surfaces.

If native-path adoption or migration fails its gate, skip only the relocation
task. Continue every later application refactor against compatibility
`ApplicationPaths`. Do not keep an unused runtime dependency or dead migration
branch.

### 5.1 Stored-schema recovery contract

The versioned persistence task uses this explicit recovery policy:

1. The current unversioned account map is schema generation zero.
2. The new document is an envelope containing `schema_version` and `accounts`.
3. Before the first automatic generation-zero rewrite, create one
   byte-for-byte sibling backup using exclusive creation.
4. Give the backup the same credential-protective permissions as the source.
5. Never overwrite an existing non-equivalent backup.
6. Write the migrated document to a same-directory temporary file, flush it,
   and atomically replace the authoritative file where supported.
7. Retain the source backup; never delete it automatically in this migration.
8. Make recovery instructions visible through typed migration output and
   `doctor` without exposing credentials.
9. After any post-upgrade write, prepare a reverse-compatible latest-state
   document before downgrade; the original backup alone is not lossless.

The implementation must finalize the exact backup filename and Windows
equivalent after the persistence spike, then inline those details in the
design before automatic migration is enabled. If safe backup and recovery
behavior cannot be proven, stored-schema auto-migration remains disabled.

### 5.2 Provider-command release contract

At the start of CS-20, record the actual first release carrying the provider
hierarchy as release `R`. If no intervening release changes the
current 0.6.0 baseline, the expected `R` is 0.7.0. Do not hard-code that
assumption before refreshing Release Please and package state.

In release `R`:

- add `claude setup-token`, `codex login`, and `codex export`;
- keep approved legacy aliases as thin delegates;
- mark aliases deprecated in help and human stderr, and verify that Release
  Please generates the matching changelog entry; and
- keep JSON, quiet, scheduled, and other machine stdout unchanged.

Keep aliases through the next minor release and remove them in the following
minor release. Record the actual version numbers before publishing `R`.

### 5.3 Operator decision ledger

Each CS-07, CS-08, CS-09, and CS-10 research commit pauses for operator
disposition. When a decision is recorded, update both the design authority and
this tracked ledger with:

- change-set id and question;
- GO or NO-GO;
- selected option or compatibility disposition;
- operator approval date;
- design commit containing the decision; and
- SHA-256 of the approved design content at that commit.

The initial design hash in this plan remains the publication baseline. The
ledger distinguishes later authorized decisions from accidental design drift.
No production dependency, writer, or native migration may rely on an
unrecorded chat-only disposition.

## 6. Testing strategy

Tests exist to protect behavior and user data, not to inflate a count.

### 6.1 Test-selection rules

For each task:

- retain an existing test when it already protects the acceptance criterion;
- add a test only when a real behavior could regress without it;
- assert typed outcomes, persisted state, emitted command, file tree, retry
  attempt, or output contract;
- prefer one clear parameterized matrix when cases share one behavior;
- avoid asserting private helper calls, incidental imports, or mock call count
  when the user-visible result is the real contract;
- use exact string equality only for intentional CLI, JSON, command, branding,
  schema, or migration contracts;
- delete redundant assertions and inert existence smoke tests; and
- keep fakes local until three test modules need the same typed harness.

Do not snapshot entire help screens or object representations. Test the stable
elements: command discovery, one masthead, ordering, output channel, and
initialization behavior.

### 6.2 Fixture rules

Use the exact reserved 30-character identity below for the identified account,
layout, and TUI replacements:

```text
long.account.name@example.test
```

Never read real credentials or the user's configuration. Build synthetic
provider payloads and account files in test-owned directories. Tokens must be
obviously fake and incapable of authenticating.

Other unrelated tests may use different reserved `.test` identities when a
different length or identity relationship is part of their behavior.

Type all test helpers and fakes. Replace test `Any` with the recursive JSON
types, concrete payload schemas, `object`, or the actual pytest type. Keep the
application wall clock and HTTP monotonic timer as separate fakes.

### 6.3 Gate ladder

Run the smallest relevant test first. A task is not complete until its focused
tests, Ruff, and type checks pass. Run the full ladder before each commit that
changes an architectural boundary:

```bash
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files
npm run lint:markdown
uv build
git diff --check
git status --short
```

Before CS-05, the Markdown command is a baseline comparison because the 93
known findings predate this plan. It may not be skipped, filtered, or declared
green. CS-05 removes that exception permanently.

Module-to-package tasks also install the built wheel into a clean environment
and inspect its contents. Migration and transport tasks additionally run their
platform matrix before activation.

### 6.4 Platform matrix

CI must prove Python 3.14 behavior on Linux, macOS, and Windows. WSL-specific
path and scheduler behavior requires a documented WSL run because a normal
Linux runner is not equivalent.

Platform tests cover only behavior that differs by platform:

- native directory discovery;
- credential and account permissions;
- atomic replacement behavior;
- provider-native credential discovery;
- proxy, CA, and TLS behavior after the transport change;
- daemon backend selection and generated artifacts; and
- wheel installation and CLI startup.

Do not duplicate all pure service tests across every operating system.

The platform gate has two stages:

1. Focused local gates permit a review-branch commit and push when authorized.
2. Linux, macOS, and Windows CI plus recorded WSL evidence must pass before
   merge to a release branch or publication.

For schema and native-location work, activation means release exposure of an
automatic writer or migration. No release containing that behavior may be
published before the platform matrix and rollback harness pass. Record CI run
URLs and immutable WSL evidence in the final parity record.

## 7. Global task protocol

Each task below follows this protocol:

- [ ] Revalidate the named files, callers, tests, and baseline at the current
      branch head.
- [ ] Search exact names before adding a type, helper, service, or dependency.
- [ ] Preserve or write the smallest load-bearing failing behavior test.
- [ ] Implement the complete task boundary without speculative hooks.
- [ ] Run focused tests, Ruff, and `ty` for touched ownership.
- [ ] Run the full gate ladder before the task's commit.
- [ ] Inspect source and built artifacts for stale files after package moves.
- [ ] Commit conventionally only after the task is independently reviewable.

The task-specific tests below supplement this protocol; they do not authorize
padding the suite.

## 8. Change sets

### 8.1 CS-00 — Persist the approved authority and plan

**Dependencies:** None.

**Files:**

- Add the approved architecture design.
- Add this implementation plan.
- Modify the design only if refreshed evidence changed before publication.

**Work:**

- [x] Refresh `develop`, `origin/develop`, worktree state, module counts,
      dependencies, command help, and quality baseline.
- [x] Confirm the design and plan contain every decision-relevant fact they
      need and reference no ignored or local-only artifact.
- [x] Confirm both documents pass targeted Markdown lint and are not ignored.
- [x] Add both documents to Git before any production implementation begins.
- [x] Record the publication commit as the execution base.

**Acceptance:**

- A fresh worker can discover both documents from the repository.
- The design remains approved and this plan has an explicit review status.
- No source, dependency, lockfile, or user-data behavior changes.

**Recovery:** Documentation-only revert.

**Commit:** `docs(architecture): add maintainable application plan`

### 8.2 CS-01 — Make daemon operation dispatch exhaustive

**Dependencies:** CS-00.

**Files:**

- Modify `src/sidekick_usages/daemon.py`.
- Modify the current CLI command owner.
- Modify `tests/test_daemon.py` and one command-boundary test only if needed.

**Work:**

- [x] Search every daemon-operation producer and consumer.
- [x] Define closed `DaemonOperation` vocabulary beside daemon operation
      behavior; do not place feature-local operations in `core/`.
- [x] Parse external strings before dispatch.
- [x] Replace the uninstall fallback with exhaustive operation matching.
- [x] Raise the existing typed usage error for unexpected input.

**Execution record:** Completed on 2026-07-09 against execution base
`73ce06891747a0571276b35c3f54c7de2c4e188f`. The focused daemon, branding,
and help suite passes 20 tests. Repository Ruff, formatting, `ty`, all 194
tests with branch coverage, pre-commit, and package build pass. The Markdown
gate remains at the exact 93-finding pre-CS-05 baseline and adds no finding.

**Load-bearing tests:**

- One parameterized test maps install, status, and uninstall to the intended
  manager method and result.
- Invalid input returns an actionable command error and proves that uninstall
  and every other backend operation remained untouched.

Do not add a test that merely lists enum values.

**Acceptance:** Unknown input can never trigger uninstall. Valid command
behavior and daemon output remain unchanged.

**Recovery:** Code-only revert; no scheduler mutation occurs in tests.

**Commit:** `fix(cli): make daemon operations exhaustive`

### 8.3 CS-02 — Preserve explicit empty heartbeat registries

**Dependencies:** CS-01.

**Files:**

- Modify the current CLI composition helper.
- Modify the smallest existing heartbeat/CLI test module that owns injection.

**Work:**

- [x] Refresh callers of `_heartbeat_providers` and registry defaults.
- [x] Replace truth-value fallback with an explicit `is None` decision.
- [x] Remove optional state only if all real composition callers provide the
      registry explicitly.
- [x] Preserve the empty mapping as a deliberate injected capability set.

**Execution record:** Completed on 2026-07-09 against
`0e318a6ba2d6c094d8099dab4940ab7c2ac16374`. The optional context field is
retained because `None` remains the explicit production-default signal. An
empty injected mapping now reaches the heartbeat command unchanged. The
focused heartbeat suite passes all 18 cases, and the full suite passes all
195 cases with branch coverage. Ruff, formatting, `ty`, pre-commit, and the
package build pass. The Markdown gate remains at the exact 93-finding
pre-CS-05 baseline and adds no finding.

**Load-bearing test:** Inject an empty registry, exercise a heartbeat command
or service resolution, assert the documented unsupported result, and prove no
production provider ran.

**Acceptance:** `None` may select defaults where the contract allows it;
`{}` never does.

**Recovery:** Code-only revert.

**Commit:** `fix(heartbeat): preserve explicit empty registries`

### 8.4 CS-03 — Establish one terminal-width policy

**Dependencies:** CS-02.

**Files:**

- Modify `src/sidekick_usages/cli_help.py`.
- Modify `src/sidekick_usages/branding.py` only as a consumer.
- Modify focused branding and help tests.

**Work:**

- [x] Search `_help_width` and every terminal-width calculation.
- [x] Make the existing help-width concept the one policy for help, masthead,
      dividers, and option layout.
- [x] Keep the policy in the help/presentation boundary, not `core/`.
- [x] Keep the approved 85-column usage-overview floor separate; it governs a
      different rendering decision.
- [x] Ensure help and version still bypass runtime composition.

**Execution record:** Completed on 2026-07-09 against
`d4e8172509b49cc16e0540f97fe97581ca4efde6`. Typer's locked 0.25.1 Rich help
constructs its own console from `rich_utils.MAX_WIDTH`; the current 0.26.8
release retains that seam. The CLI adapter now resolves width once, applies it
to Click, the masthead, and Typer's Rich console, then restores Typer state in
`finally`. The focused help, branding, and usage-render suite passes all 45
cases, including 40-, 85-, and 120-column help plus non-leaking error output.
The independent usage-overview floor remains unchanged. The full suite passes
all 198 cases with branch coverage; Ruff, formatting, `ty`, pre-commit, and the
package build pass. A clean wheel installation resolved Typer 0.26.8 and ran
help and version successfully. The Markdown gate remains at the exact
93-finding pre-CS-05 baseline and adds no finding. See the canonical
[Typer 0.26.8 release notes][typer-0268-release] and
[Rich console source][typer-0268-rich-width].

**Load-bearing tests:**

- At narrow width, the masthead degrades without wrapping and help agrees.
- At 80 or 85 columns, the help and masthead use the same resolved width while
  the usage overview retains its approved independent floor.
- At 120 columns, the masthead, divider, and help layout no longer disagree.
- Version remains one undecorated line.

Avoid whole-screen snapshots. Assert width, ordering, and stable copy only.

**Acceptance:** One width policy owns the shared CLI frame; no machine output
changes.

**Recovery:** Code-only revert.

**Commit:** `fix(help): share terminal width policy`

### 8.5 CS-04 — Remove verified dead and speculative surfaces

**Dependencies:** CS-03.

**Files:**

- Modify the live owners identified by refreshed caller searches.
- Modify tests only where a user-visible contract is removed or corrected.

**Work:**

- [x] Revalidate and remove the no-op doctor `--auth` option if it remains
      unused.
- [x] Revalidate and remove `diagnostic_dicts` if it remains uncalled.
- [x] Revalidate and remove `providers.get_provider` if explicit registry
      construction remains the real owner.
- [x] Remove unused `acct` parameters from the two named helpers if they
      remain unused.
- [x] Stop over-exporting `heartbeat_status_dict` if it remains private to one
      renderer.
- [x] Replace the one-caller parameterized `brand_line(section)` with a
      specifically named update-status builder if caller evidence is unchanged.
- [x] Correct or delete every stale comment and docstring listed in design
      section 15.8.
- [x] Defer removal of generic `Provider.run_setup_token` to the provider
      package task unless it can be replaced directly with the final
      Claude-owned capability now; do not create a temporary abstraction.

**Execution record:** Completed on 2026-07-09 against
`96b7a52ce107b6cfc4a0fdfc4a594ac89d63aee6`. Exact caller searches proved
the doctor option was discarded, `diagnostic_dicts` and `get_provider` were
uncalled, both `acct` parameters were unused, and the heartbeat status builder
had only renderer-local consumers. The status brand now has the specific
parameterless name `update_status_line()`. `Provider.run_setup_token` remains
unchanged because it is a live command capability; its final Claude ownership
and generic-contract removal remain assigned to CS-16 and CS-17A. One new
public-help test proves doctor no longer advertises `--auth`; no name-absence
or other filler tests were added. The focused behavioral suite passes all 87
cases, and the full suite passes all 199 cases with branch coverage. Ruff,
formatting, `ty`, pre-commit, and the package build pass. The Markdown gate
remains at the exact 93-finding pre-CS-05 baseline and adds no finding.

**Load-bearing tests:**

- Doctor help no longer advertises a no-op option.
- Existing doctor, update, heartbeat, and provider behavior remains covered by
  their current public tests.

Do not add tests that only assert private Python names are absent. Exact caller
searches, Ruff, and `ty` prove dead-code removal.

**Acceptance:** No supported user behavior disappears, and no speculative
compatibility export remains.

**Recovery:** Code-only revert.

**Commit:** `chore: remove dead surfaces and stale comments`

### 8.6 CS-05 — Sanitize fixtures and make Markdown green

**Dependencies:** CS-04.

**Files:**

- Modify affected tests and documentation found by a refreshed identity scan.
- Correct the two June TUI documents without altering their approved design.

**Work:**

- [x] Replace the identified account, layout, and TUI identities with exactly
      `long.account.name@example.test` so sanitization retains the intended
      30-character width case.
- [x] Remove any instruction that would mutate a real saved account or active
      provider login.
- [x] Wrap the 93 baseline long lines and fix the existing code-span spacing
      finding.
- [x] Preserve code blocks, terminal art, and intentional product copy.
- [x] Run Markdown lint over all repository Markdown, not only touched files.

**Execution record:** Completed on 2026-07-09 against
`7d8e4d9c69afa031a484736add6411aa771ea78c`. The refreshed identity scan found
the person-derived labels only in the two renderer/error fixtures and the two
June TUI documents. The binding fixture now uses the exact reserved
30-character identity. Distinct reserved `.test` labels remain only where the
multi-account relationship is itself part of the rendering behavior, as
allowed by section 6.2. The documents no longer direct an operator to mutate a
saved account or use the potentially state-changing live `check` command for
acceptance. The terminal mockup retains its 74-cell panel rows and stable
product copy. A Prettier `proseWrap=always` preview was rejected because it
also rewrote unrelated emphasis, tables, and list formatting; the existing
Markdown gate was used for the required prose-only repair instead. See the
canonical [Prettier prose-wrap option][prettier-prose-wrap]. No test was added:
the existing focused documentation, branding, rendering, and error tests are
the load-bearing behavior boundary, and only their reserved fixtures changed.
Those 41 focused cases pass. The full suite passes all 199 cases with branch
coverage; Ruff, formatting, `ty`, pre-commit, the package build, and
repository-wide Markdown lint also pass. Markdown lint now reports zero
findings, permanently removing the pre-CS-05 baseline exception.

**Load-bearing verification:**

```bash
npm run lint:markdown
uv run pytest tests/test_docs.py tests/test_branding.py tests/test_render.py
```

Expected outcome: repository Markdown is green and the approved TUI tests are
unchanged in behavior.

**Recovery:** Documentation and fixture-only revert.

**Commit:** `docs: clean identities and markdown gates`

### 8.7 CS-06 — Clean and enable focused hygiene gates

**Dependencies:** CS-05.

**Files:**

- Modify the focused source and test findings.
- Modify `pyproject.toml`.
- Modify `.pre-commit-config.yaml` only where the owning gate changes.

**Work:**

- [ ] Fix every refreshed `E501` and `W505` finding before enabling the rule.
- [ ] Remove the unused global `B905` ignore.
- [ ] Remove each current `# noqa` by fixing the cause; retain only a
      rule-specific, one-line justified suppression when unavoidable.
- [x] Remove legacy future-annotations imports and align `AGENTS.md` with
      native Python 3.14 deferred annotations. This gate correction was pulled
      forward during CS-01 after `pyupgrade --py314-plus` enforced it.
- [ ] Record, but do not broadly enable, annotation rules whose current
      baseline is still noisy.

**Acceptance:**

- The enabled line-length and warning rules start from zero findings.
- No blanket suppression is added.
- The full existing behavior suite stays green.

**Recovery:** Revert code formatting and gate configuration together.

**Commit:** `chore(quality): enable clean hygiene gates`

### 8.8 CS-07 — Decide the schema-validation dependency

**Dependencies:** CS-06.

**Files:**

- Modify the design authority with measured results and final decision.
- Add tracked schema-validation research only if the inlined evidence would
  make the design unreadable.
- Do not modify production dependencies in this change set.

**Work:**

- [ ] Refresh primary-source and canonical-repository evidence for standard
      parsing, Pydantic `TypeAdapter`, cattrs, and msgspec.
- [ ] Build a synthetic corpus covering current Sidekick records, prototype
      records, provider payloads, JWT claims, and refresh responses.
- [ ] Measure strict missing, extra, null, mistyped, and coercing behavior.
- [ ] Compare error paths, schema migration ergonomics, secret redaction,
      startup, decoding cost, wheel size, Homebrew packaging, and platforms.
- [ ] Record transitive dependencies, license, advisories, provenance,
      maintainer health, release posture, and reversal conditions.
- [ ] Select one boundary-validation approach and inline the decision.

**GO gate:** One option provides strict typed validation and actionable paths
without leaking its framework into `core/` or creating unacceptable packaging
or maintenance cost.

**STOP gate:** No candidate meets the real payload, security, packaging, and
platform requirements. Reopen design section 6.4; do not improvise a schema
framework or add a dependency.

This decision never approves `pydantic-settings`.

**Decision authority:** After committing the evidence, set this change set to
`WAITING FOR OPERATOR DECISION`. Record the operator-approved GO or NO-GO and
selected approach in the design authority before any production consumer or
dependency change proceeds.

**Commit:** `docs(research): decide schema validation dependency`

### 8.9 CS-08 — Decide HTTP transport and retry ownership

**Dependencies:** CS-06.

**Files:**

- Modify the design authority with measured results and final decision.
- Add tracked HTTP research only if supplementary evidence is warranted.
- Do not modify production dependencies in this change set.

**Work:**

- [ ] Refresh urllib3, Tenacity, and focused-local-option evidence from
      official docs, releases, security policies, and canonical repositories.
- [ ] Exercise the four real request capabilities and current typed errors.
- [ ] Classify the Claude probe, Claude refresh, Codex refresh, Claude
      heartbeat, and Codex heartbeat POST operations.
- [ ] Record retry eligibility after connect failure, ambiguous read failure,
      429, selected 5xx, and explicit provider rejection for each operation.
- [ ] Compare final 429 metadata, integer and HTTP-date `Retry-After`, bounded
      deadlines, deterministic tests, pooling, TLS, proxy, CA, redirects,
      startup, wheel, Homebrew, platforms, license, and maintenance.
- [ ] Prove each candidate can use independently injected aware wall time for
      absolute HTTP dates and monotonic time for elapsed retry budgets.
- [ ] Select urllib3 retry, Tenacity over retry-disabled urllib3, or focused
      local retry over retry-disabled urllib3 as the only retry owner.
- [ ] Inline the decision, operation-safety table, and reversal conditions.

**GO gate:** One pooled design has exactly one retry owner, preserves typed
errors and final rate-limit guidance, and passes the operation-safety matrix.

**STOP gate:** No pooled candidate meets the contract. Reopen HTTP design;
never stack retry engines or silently retain a non-pooled target.

**Decision authority:** After committing the evidence, set this change set to
`WAITING FOR OPERATOR DECISION`. Record the operator-approved GO or NO-GO and
selected transport/retry owner in the design authority before HTTP production
work proceeds.

**Commit:** `docs(research): decide HTTP transport and retry dependency`

### 8.10 CS-09 — Decide native application paths

**Dependencies:** CS-06.

**Files:**

- Modify the design authority with exact platform outputs and decision.
- Add tracked application-path research only if supplementary evidence is
  needed.
- Do not add `platformdirs` to production in this change set.

**Work:**

- [ ] Refresh `platformdirs` release, canonical repository, license,
      advisories, provenance, maintenance, and Python/platform support.
- [ ] Record exact Linux, macOS, Windows, and WSL outputs for the chosen
      application name, author, roaming, and override policy.
- [ ] Verify `ensure_exists=False` produces no discovery side effect.
- [ ] Confirm account state and private Codex auth map to durable data while
      lifetime totals map to cache.
- [ ] Define exact canonical, existing Sidekick, prototype, private Codex, and
      lifetime-cache locations.
- [ ] Record packaging impact, migration feasibility, and reversal conditions.

**GO gate:** Exact native paths and a safe compatibility transition are
approved for every supported environment.

**STOP gate:** Retain compatibility paths. Later refactors continue without a
direct `platformdirs` dependency or dormant native-migration code.

**Decision authority:** After committing the evidence, set this change set to
`WAITING FOR OPERATOR DECISION`. Record the operator-approved GO or NO-GO,
exact compatibility disposition, and any canonical paths in the design
authority. Compatibility-only path injection may proceed after either recorded
disposition; native migration requires a recorded GO.

**Commit:** `docs(research): decide application path discovery dependency`

### 8.11 CS-10 — Finalize stored-schema and recovery contracts

**Dependencies:** Recorded operator CS-07 GO and recorded operator CS-09 GO or
NO-GO disposition.

**Files:**

- Modify the design authority.
- Add tracked persistence research only if supplementary evidence is needed.
- Do not enable a production schema writer in this change set.

**Work:**

- [ ] Capture exact synthetic examples of the current unversioned Sidekick
      account map and the prototype `cc-usage` shape.
- [ ] Define the first versioned envelope with exactly two top-level fields:
      `schema_version` and `accounts`.
- [ ] Define strict schemas for every supported input generation and reject
      unknown future versions.
- [ ] Finalize the generation-zero backup filename and Windows permission
      behavior described in section 5.1.
- [ ] Define the atomic same-directory write, flush, replacement, directory
      durability, and partial-write recovery sequence per supported platform.
- [ ] Define behavior when an equivalent or conflicting backup already exists.
- [ ] Define old-binary rollback instructions and how `doctor` reports them
      without printing secrets.
- [ ] Define how rollback works after a later native-location migration and
      after new writes exist only in the canonical generation.
- [ ] Design a lossless reverse transformation or rollback-preparation surface
      that converts the latest supported state for `v0.6.0`; restoring only
      the pre-upgrade backup is insufficient after new writes.
- [ ] Define whether schema migration occurs automatically at load or through
      an explicit pre-load coordinator step; choose one owner only.
- [ ] Inline the complete contract and obtain approval before CS-14 writes it.

**Required contract tests to design now:**

- generation-zero input to the exact new envelope;
- prototype input through its explicit supported transformation;
- unknown future version rejection;
- unreadable and malformed input;
- backup creation, equivalence, collision, and permissions;
- interruption before backup, before replace, and after replace;
- idempotent restart after every interruption point; and
- documented recovery by the previous released binary.
- a post-upgrade account change followed by rollback preparation, proving the
  previous release reads the latest representable state without data loss.

**GO gate:** The new writer cannot destroy the only trustworthy copy, and a
user can recover after binary rollback without guessing which generation is
authoritative.

**STOP gate:** Automatic schema migration remains disabled. Do not treat
atomic replacement alone as rollback support.

**Decision authority:** After committing the contract, set this change set to
`WAITING FOR OPERATOR DECISION`. Record operator-approved GO or NO-GO,
including the exact rollback-preparation surface and any intentionally
unrepresentable field policy, in the design authority. CS-14 automatic writing
requires a recorded GO. Clock, HTTP, compatibility paths, core, and lifetime
work may continue after NO-GO; persistence-dependent work waits for a revised
approved contract.

**Commit:** `docs(persistence): define schema migration recovery contract`

### 8.12 CS-10A — Establish explicit application wall time

**Dependencies:** CS-06.

**Files:**

- Create `src/sidekick_usages/clock.py`.
- Modify the current composition root.
- Modify current maintenance, heartbeat, doctor, provider, CLI, and renderer
  call sites that acquire wall time.
- Add focused clock and explicit-reference-time tests.

**Work:**

- [ ] Define the narrow `Clock` protocol and production `SystemClock` returning
      aware UTC `datetime` values.
- [ ] Construct one application wall clock at the current composition root.
- [ ] Inject it into current services and provider adapters that acquire time.
- [ ] Pass an explicit reference time into current renderers instead of
      allowing them to call `datetime.now()`.
- [ ] Leave persistence and provider-native string encoders at their current
      boundaries until CS-13 normalizes models and expiry.
- [ ] Do not put sleeping, monotonic time, scheduler behavior, parsing, or
      timestamp formatting in `clock.py`.

**Load-bearing tests:**

- `SystemClock.now()` returns aware UTC.
- A service samples its fake clock once for one decision.
- A renderer receives reference time and never acquires system time.
- Existing stored/provider timestamp strings remain behaviorally unchanged.

**Acceptance:** Direct wall-time acquisition remains only in `SystemClock`.
The later HTTP package can consume aware wall time for HTTP-date evaluation
without inventing a temporary clock abstraction.

**Recovery:** Code-only revert; no timestamp representation or stored data
changes.

**Commit:** `refactor(time): inject explicit application wall time`

### 8.13 CS-11 — Replace `http.py` with the pooled HTTP package

**Dependencies:** Recorded CS-07 GO, recorded CS-08 GO, and CS-10A.

**Files:**

- Create `src/sidekick_usages/serialization/__init__.py`.
- Create `src/sidekick_usages/serialization/json.py`.
- Create `src/sidekick_usages/http/__init__.py`.
- Create `src/sidekick_usages/http/client.py`.
- Create `src/sidekick_usages/http/retry.py`.
- Delete `src/sidekick_usages/http.py` atomically.
- Modify `src/sidekick_usages/errors.py`.
- Modify every current HTTP consumer and the flat composition root.
- Create `packaging/smoke_wheel.py` as the reusable cross-platform artifact
  verifier before this first module-to-package conversion.
- Modify `pyproject.toml` and `uv.lock` for approved dependencies.
- Refocus `tests/test_http_errors.py`; split it only if client and retry
  behavior are clearer as two cohesive modules.
- Modify packaging tests.

**Work:**

- [ ] Add recursive JSON aliases and strict runtime object narrowing without
      an unchecked cast.
- [ ] Add only the approved boundary-validation and transport/retry
      dependencies; do not add alternative candidates.
- [ ] Preserve `from sidekick_usages.http import HttpClient`.
- [ ] Preserve the four capability-oriented request methods unless refreshed
      callers justify a smaller named surface.
- [ ] Construct requests with standard-library `HTTPMethod` members, never
      magic method strings.
- [ ] Return response headers as a documented Sidekick-owned normalized
      mapping; expose no selected-library header or response type.
- [ ] Create one pool per CLI invocation under transactional lifecycle
      ownership, retaining cleanup until composition safely transfers it.
- [ ] Keep help and version from constructing the pool.
- [ ] Enforce HTTPS before transport access.
- [ ] Bound request bodies, response reads, connect/read timeouts, attempts,
      elapsed deadline, jitter, and server-directed waits.
- [ ] Use the closed operation-safety policies approved in CS-08.
- [ ] Parse non-negative delay seconds and HTTP-date `Retry-After` values,
      apply the product cap, and retain the last valid delay in exhaustion.
- [ ] Use the injected `Clock` only to evaluate absolute HTTP dates and a
      distinct injected monotonic source for elapsed deadlines.
- [ ] Translate all selected-library and transport exceptions into Sidekick
      errors.
- [ ] Redact tokens, authorization headers, payloads, and full identities from
      errors and any retry observation.
- [ ] Keep retry observation absent until a real consumer exists.
- [ ] Remove or revise the current `B310` suppression whose justification is
      specific to the retired urllib request implementation.
- [ ] Remove the old module and confirm no direct transport or retry import
      exists outside `http/`.
- [ ] Make the verifier build into a fresh test-owned output directory, require
      the exact resulting wheel, inspect package members, install that wheel in
      isolation, clear source leakage, and exercise both entry paths.
- [ ] Update the verifier's required/forbidden package manifest in every later
      same-named module-to-package conversion.

**Load-bearing tests:**

- Non-HTTPS input raises `InsecureUrlError` before the fake transport runs.
- Object JSON succeeds; malformed JSON, list, scalar, and `null` fail with the
  typed payload error.
- Oversized bodies fail through a bounded read.
- A 401 and 403 fail immediately with the existing semantic errors.
- Approved 429 and selected 5xx paths stop at attempt or elapsed limits.
- Integer and HTTP-date `Retry-After`, past dates, malformed values, and the
  cap select the documented waits.
- Independently controlled wall and monotonic clocks prove that wall-clock
  movement cannot extend an elapsed retry budget.
- Safe and unsafe POST operations behave differently after an ambiguous send.
- An injected monotonic timer stops retries while attempts remain.
- Pool closure occurs once at invocation teardown and not for help/version.
- A typed failure after pool creation but before composition completes closes
  the pool exactly once and preserves the original error.
- Header-returning requests yield the normalized Sidekick mapping without a
  transport or retry-library type.
- Errors contain no synthetic credential value or authorization header.
- Existing JSON, quiet, and scheduled output remains unchanged.

Assert Sidekick behavior and transport requests, not selected-library private
types.

**Acceptance:**

- Exactly one retry owner exists.
- Request construction uses `HTTPMethod` and the package boundary exposes no
  selected-library request, response, or header type.
- Every provider and feature imports only the Sidekick `HttpClient` facade.
- The source tree and wheel contain `http/` and no stale `http.py`.
- The dependency lock and packaging tests describe the selected implementation.
- `uv run python packaging/smoke_wheel.py --build` proves the exact newly built
  artifact without reading or deleting pre-existing files under `dist/`.

**Recovery:** Revert source, dependency declaration, and lockfile together.
No durable data changes.

**Commit:** `refactor(http): centralize transport and retry policy`

### 8.14 CS-12 — Inject current Sidekick-owned paths without relocation

**Dependencies:** A recorded operator GO or NO-GO for CS-09. This task proceeds
with compatibility paths under either disposition.

**Files:**

- Create `src/sidekick_usages/paths.py`.
- Modify `src/sidekick_usages/store.py`.
- Modify `src/sidekick_usages/lifetime.py`.
- Modify current private Codex path construction in `cli.py`.
- Modify the current composition root.
- Add `tests/test_paths.py` and adjust focused consumer tests.

**Work:**

- [ ] Implement frozen `AccountLocations`, `PrivateCodexLocations`, and
      `ApplicationPaths` exactly as approved.
- [ ] Resolve current canonical and existing Sidekick paths to the same
      compatibility locations in this task.
- [ ] Retain the distinct prototype account source.
- [ ] Inject the account path into `AccountStore` and the lifetime cache path
      into its owner.
- [ ] Inject the existing and canonical private Codex roots into the current
      credential workflow.
- [ ] Remove Sidekick-owned import-time path singletons and duplicate
      `Path.home()` reconstruction.
- [ ] Keep Claude/Codex native homes provider-owned and daemon installation
      paths daemon-owned.
- [ ] Ensure discovery creates no directory or file.
- [ ] Do not add or import `platformdirs` unless CS-09 and later CS-19 approve
      native relocation.

**Load-bearing tests:**

- The compatibility resolver produces the exact current account, private
  Codex, and lifetime-cache locations.
- Canonical and existing compatibility locations are equal and deduplicated.
- The prototype location remains distinct.
- Discovery has no filesystem side effect.
- A frozen injected `ApplicationPaths` drives store and lifetime behavior in
  a test-owned root.
- Provider-native homes and daemon installation paths are absent from the
  value.

Do not test `platformdirs` outputs in this compatibility-only task.

**Acceptance:** Existing users read and write the same physical files as
before. No migration runs and no directory is created by discovery.

**Recovery:** Code-only revert; no file moved.

**Commit:** `refactor(paths): inject current sidekick-owned paths`

### 8.15 CS-12A — Establish proven shared type vocabulary

**Dependencies:** CS-12.

**Files:**

- Create `src/sidekick_usages/core/__init__.py`.
- Create `src/sidekick_usages/core/types.py` only for vocabulary proven now.
- Modify current callers of the selected shared types.
- Update focused type and behavior tests.

**Work:**

- [ ] Add only shared aliases or enums with current cross-feature consumers,
      such as proven `ProviderId` and exit/status vocabulary.
- [ ] Leave `Account`, `DetectedCredentials`, `UsageWindow`, and `UsageReport`
      in their current owners until CS-13 can normalize all time-bearing and
      provider-shaped fields atomically.
- [ ] Keep validation, provider parsing, persistence, Rich, Typer, HTTP, paths,
      settings, filesystem, and clocks out of core.
- [ ] Keep secret-bearing credential fields out of default representations.

**Load-bearing tests:** Selected enums/aliases preserve their documented string
or integer boundary behavior, reject unsupported closed values where intended,
and satisfy current consumers. Import checks prove the narrow core boundary.

**Recovery:** Atomic behavior-preserving model-move revert.

**Commit:** `refactor(core): centralize proven shared types`

### 8.16 CS-13 — Move Account and normalize expiry atomically

**Dependencies:** Recorded CS-07 GO, CS-10A, CS-11, CS-12, and CS-12A.

This is one atomic semantic boundary. Do not commit a transitional core model
that stores provider-native integers or formatted timestamps.

**Files:**

- Create `src/sidekick_usages/core/expiry.py`.
- Create `src/sidekick_usages/core/models.py`.
- Delete `src/sidekick_usages/report.py` atomically.
- Modify `store.py`, provider modules, maintenance, doctor, heartbeat, CLI,
  lifetime, and render consumers.
- Add focused core/time tests and update existing behavior tests.

**Caller-driven decision checkpoint:**

- [ ] Decide `AccountLabel` as an alias, `NewType`, or value object from real
      validation and normalization behavior; default to the simplest type
      that enforces proven invariants.
- [ ] Decide the smallest account/credential representation that prevents
      provider-incompatible combinations without a speculative class tree.
- [ ] Select a discriminated expiry representation that cannot express valid
      or expired without an aware time, or unknown with an authoritative time.
- [ ] Decide which refresh, heartbeat, and provider statuses have enough
      cross-module consumers to move to enums now.
- [ ] Inline any material refinement to the design before coding it.

**Work:**

- [ ] Move `Account`, `DetectedCredentials`, `UsageWindow`, and `UsageReport`
      to core only with their final aware-time and provider-neutral fields.
- [ ] Remove the raw provider JSON dictionary from `UsageReport`; provider
      schemas retain boundary payloads without leaking them into core.
- [ ] Normalize all runtime expiry and audit time values to aware UTC
      datetimes or explicit discriminated states.
- [ ] Convert Claude milliseconds and Codex seconds at provider boundaries.
- [ ] Keep legacy stored-unit conversion in the current persistence boundary
      until CS-14 writes the versioned schema.
- [ ] Use the CS-10A clock and acquire one `now` per policy decision before
      passing it to pure expiry logic.
- [ ] Keep HTTP elapsed deadlines on the separate monotonic source from CS-11.
- [ ] Keep provider-native, persisted, and human timestamp encoders with their
      owners; remove the three `_now_utc_z()` duplicates without creating
      `timestamps.py`.
- [ ] Ensure secret-bearing fields are absent from default representations.

**Load-bearing tests:**

- Expiry immediately before, exactly at, and immediately after its boundary
  produces the documented state from one explicit `now`.
- Malformed or invalid provider expiry input becomes the typed invalid/failure
  state and never epoch, zero remaining, or a plausible default.
- Unknown and invalid expiry states cannot carry contradictory authoritative
  timestamps.
- Claude and Codex native units normalize to the same runtime representation.
- Provider-native timestamp encoding, legacy persistence encoding, and human
  formatting remain separate and behaviorally correct.
- A service uses one clock sample for one decision.
- HTTP deadlines remain deterministic when wall time moves.
- Core model representations never reveal synthetic token values.
- Import checks prove core has no provider, persistence, HTTP, path,
  filesystem, settings-loader, Rich, Typer, or clock dependency.
- Source and wheel contain the core models and no stale `report.py`.

**Acceptance:** All current account, usage, maintenance, heartbeat, doctor, and
rendering behavior passes with final runtime time types. No invalid
provider-native time representation enters core.

**Recovery:** Atomic code-only revert. The persisted file remains in its
existing compatible shape until CS-14.

**Commit:** `refactor(core): normalize models and expiry policy`

### 8.17 CS-13A — Make lifetime collection failures explicit

**Dependencies:** CS-13.

**Files:**

- Modify `src/sidekick_usages/lifetime.py`.
- Modify its CLI/service consumers and `tests/test_lifetime.py`.

**Work:**

- [ ] Define a feature-local typed result distinguishing valid totals,
      unavailable source data, and invalid/read/write failures.
- [ ] Preserve valid zero as a real total, never an error sentinel.
- [ ] Surface malformed/unreadable Claude statistics and Codex rollouts as
      actionable typed states.
- [ ] Surface cache read and write failures without pretending that the cache
      is empty or successfully updated.
- [ ] Keep Claude/Codex native source locations lifetime/provider-owned and use
      injected `ApplicationPaths` only for the Sidekick-owned cache.
- [ ] Keep token/date human formatting at presentation boundaries.

**Load-bearing tests:**

- Valid zero, non-zero, missing, unreadable, malformed, and failed cache write
  each produce their distinct documented state.
- A failed rollout read cannot lower or claim a valid total silently.
- Renderers receive completed lifetime state and perform no filesystem I/O.

**Acceptance:** No lifetime parse or I/O failure becomes `(0, None)`, an empty
cache, or a claimed successful write.

**Recovery:** Code-only revert; cache remains regenerable and durable account
state is untouched.

**Commit:** `fix(lifetime): preserve collection failure states`

### 8.18 CS-14 — Replace `store.py` with typed, versioned persistence

**Dependencies:** Recorded operator CS-07 GO, recorded operator CS-10 GO,
CS-12, CS-13, and CS-13A.

**Files:**

- Create `src/sidekick_usages/persistence/__init__.py`.
- Create `src/sidekick_usages/persistence/account_store.py`.
- Create `src/sidekick_usages/persistence/schemas.py`.
- Create `src/sidekick_usages/persistence/migrations.py`.
- Delete `src/sidekick_usages/store.py` atomically.
- Modify every store consumer and test import.
- Modify `src/sidekick_usages/doctor.py` and its tests for pre-load schema
  assessment and recovery guidance.
- Modify `pyproject.toml` and `uv.lock` only if the approved validator has not
  already entered through CS-11.
- Add cohesive persistence and schema-migration tests.

**Work:**

- [ ] Encode the exact versioned envelope approved in CS-10.
- [ ] Strictly validate the unversioned current map, prototype shape, and every
      explicitly supported version.
- [ ] Reject malformed, unreadable, partially migrated, and unknown future
      state with typed actionable errors.
- [ ] Implement pure schema transformations before filesystem coordination.
- [ ] Implement the approved exclusive backup and recovery contract.
- [ ] Implement same-directory atomic writes and credential-protective
      permissions where supported.
- [ ] Add explicit `persist(account)` and replace every production
      `upsert(account)` plus `save()` sequence.
- [ ] Keep internal in-memory mutations private and unambiguously named.
- [ ] Reuse `filter_by_provider()` rather than retaining manual duplicates.
- [ ] Define read-only stored-schema assessment in
      `persistence/migrations.py` without activating native relocation.
- [ ] Let `doctor` assess generation zero, current generation, malformed
      input, backup state, and interrupted migration without constructing or
      loading `AccountStore`.
- [ ] Keep `paths.py` discovery-only and persistence free of provider imports.
- [ ] Leave physical locations unchanged.

**Load-bearing tests:**

- Empty initial state only when no candidate exists.
- Exact current and prototype inputs migrate to the exact versioned envelope.
- Each supported version loads; unknown future versions fail closed.
- Wrong top-level shape, non-object account, missing required field, wrong
  scalar type, unreadable file, and malformed JSON remain distinct errors.
- A literal string such as `"false"` cannot coerce to Boolean true.
- Migration interruption at each approved checkpoint is recoverable and
  idempotent.
- Backup equivalence, collision, and permissions follow CS-10.
- `persist()` updates memory and durable state together; failed writes leave
  the last valid file and in-memory contract consistent.
- Callers cannot accidentally perform a public in-memory-only update.
- Persistence imports no provider package and exposes no validation framework
  type to core.
- `doctor` reports generation, backup equivalence or collision, interruption,
  and exact recovery action without loading or mutating invalid state.
- An isolated compatibility harness builds or installs the local `v0.6.0` tag,
  migrates a synthetic generation-zero store, makes a new-schema account
  change, runs the approved rollback preparation, and proves the released old
  binary reads the latest representable state.
- Source and wheel contain `persistence/` and no stale `store.py`.

**Acceptance:** Malformed or failed migration never appears as an empty valid
store. The old physical location remains authoritative. The approved reverse
path recovers the actual `v0.6.0` release after post-upgrade writes, not merely
the pre-upgrade snapshot or a current-code fixture parser.

**Recovery:** Before code rollback, use the CS-10 reverse preparation for the
latest state; use the original backup only when no post-upgrade change must be
preserved. A Git revert alone is insufficient after the new schema is written.

**Commit:** `refactor(store): validate and persist accounts explicitly`

### 8.19 CS-15 — Extract the typed usage application service

**Dependencies:** CS-14. Native relocation is not required.

**Files:**

- Create `src/sidekick_usages/usage/__init__.py`.
- Create `src/sidekick_usages/usage/models.py`.
- Create `src/sidekick_usages/usage/service.py`.
- Modify the current flat CLI adapter.
- Modify maintenance/provider interactions only where ownership moves.
- Move relevant cases from `tests/test_check_errors.py` and
  `tests/test_cli_refresh.py` into a cohesive usage-service test module.
- Retain a small number of command-boundary integration tests.

**Work:**

- [ ] Revalidate `_do_check`, `_fetch_and_render`, refresh helpers, collection
      state, provider filtering, and persistence calls.
- [ ] Define immutable `UsageCheckResult`, account-usage result, and typed
      `FetchFailure` shapes from real callers.
- [ ] Move account selection, collection, refresh-and-retry orchestration,
      plan/account-id updates, explicit persistence, and failure aggregation
      into `UsageCheckService`.
- [ ] Reuse the maintenance refresh workflow where semantics match; remove the
      duplicate `_refresh_and_save` path rather than creating another service.
- [ ] Preserve partial success without printing or raising `typer.Exit`.
- [ ] Remove collected reports, failures, provider filter, and command-local
      state from the current `AppContext`.
- [ ] Leave human rendering, error-channel selection, and exit mapping in the
      command adapter. The usage check remains human-only; this plan does not
      add `check --json`.
- [ ] Remove private-helper tests after the public service and command tests
      cover their behavior.

**Load-bearing tests:**

- All selected accounts succeed and produce immutable results.
- One provider fails while another succeeds; both success and failure remain
  explicit.
- Known expiry refreshes before first fetch.
- A 401 triggers the approved provider refresh workflow and one fetch retry.
- Refresh rejection, forbidden response, rate limit, and transient exhaustion
  produce their typed application failures.
- Successful provider plan or account-identity changes persist once.
- Failed persistence is reported and never presented as successful usage.
- Provider filtering reuses the store query contract.
- The CLI adapter renders the existing human usage output and maps the
  documented exit status without changing service data.

Do not assert internal helper calls. Assert provider requests, result values,
durable state, rendered contract, and exit outcome.

**Acceptance:** `usage/service.py` imports no Typer, Rich, CLI, or renderer.
The existing grouped overview remains unchanged. Existing doctor and heartbeat
machine modes remain outside this usage service.

**Recovery:** Code-only revert; no schema or path change belongs here.

**Commit:** `refactor(usage): extract typed usage check service`

### 8.20 CS-15A — Rename heartbeat models and ports without behavior change

**Dependencies:** CS-15.

**Files:**

- Rename `heartbeat/domain.py` to `heartbeat/models.py`.
- Rename `heartbeat/base.py` to `heartbeat/ports.py`.
- Update heartbeat, provider, CLI, and test imports atomically.
- Keep concrete heartbeat adapters in their current locations until the
  provider package moves.

**Work:**

- [ ] Preserve the existing heartbeat models, port contract, target selection,
      service behavior, rendering, and exit policy.
- [ ] Keep initializers thin and export only currently supported public names.
- [ ] Remove the old files in the same commit and inspect the wheel.
- [ ] Make no refresh, credential, registry, or provider error-contract change.

**Load-bearing tests:** Existing heartbeat service, command, target-cache, and
provider-adapter behavior passes through the new imports. The wheel contains
`models.py` and `ports.py` and no stale `domain.py` or `base.py`.

**Recovery:** Atomic behavior-preserving rename revert.

**Commit:** `refactor(heartbeat): name models and ports explicitly`

### 8.21 CS-16 — Package Claude without changing behavior

**Dependencies:** CS-13 through CS-15 and CS-15A.

**Files:**

- Convert `providers/claude.py` atomically into `providers/claude/` with
  `provider.py`, `credentials.py`, `usage.py`, `heartbeat.py`, and
  `schemas.py`.
- Update provider and heartbeat registry imports.
- Update current CLI/service imports.
- Update Claude-provider path references in `docs/debugging-claude.md` in this
  same commit.
- Refocus Claude, scope, header-path, refresh, and heartbeat tests.

**Work:**

- [ ] Move the concrete Claude heartbeat adapter under Claude ownership.
- [ ] Put platform credential discovery and file parsing in
      `claude/credentials.py`.
- [ ] Put usage routes, scope rules, requests, and response conversion in
      `claude/usage.py`.
- [ ] Move current payload parsing/validation and already-normalized time
      conversion into `claude/schemas.py` without changing accepted inputs,
      coercion, or failures.
- [ ] Keep the current Boolean refresh contract and observable behavior in
      `provider.py` until CS-17A replaces it atomically for both providers.
- [ ] Move the current setup-token implementation under Claude ownership while
      preserving its public behavior; CS-17A narrows the generic contract.
- [ ] Keep interactive token input outside the provider package.
- [ ] Keep package initializers thin and export only supported facades.
- [ ] Remove old modules in the same atomic package conversion.
- [ ] Defer strict validator adoption, typed refresh, safe error taxonomy, and
      generic setup-token contract removal exclusively to CS-17A.

**Load-bearing tests:**

- Inference-header and OAuth usage routes retain their scope behavior.
- Existing credential detection and refresh behavior remains characterized
  through the public facade; semantic corrections land in CS-17A.
- CLI-based refresh rejection does not silently select a different workflow.
- Setup-token remains discoverable under its current public command.
- Claude heartbeat preserves active/inactive window behavior.
- No provider test reads a real credential location.

**Acceptance:** Claude code imports no CLI, Typer, Rich renderer, persistence
implementation, or selected HTTP library. Provider-native time stops at its
schemas. The wheel contains `providers/claude/` and no stale `claude.py`.

**Recovery:** Revert the complete Claude package move. No persistent-format
change belongs here.

**Commit:** `refactor(claude): create provider-owned integration package`

### 8.22 CS-17 — Package Codex and complete explicit provider wiring

**Dependencies:** CS-16.

**Files:**

- Convert `providers/codex.py` atomically into `providers/codex/` with
  `provider.py`, `auth.py`, `usage.py`, `heartbeat.py`, and `schemas.py`.
- Create or finalize `providers/registry.py`.
- Delete the old concrete heartbeat modules and registry after imports move.
- Update current CLI/service imports.
- Refocus Codex provider, refresh, scope, and heartbeat tests.

**Work:**

- [ ] Put `CODEX_HOME`, `auth.json`, JWT claims, identity matching, imports,
      exports, private per-account copies, permissions, and Codex-native time
      formatting in `codex/auth.py`.
- [ ] Put usage requests and response conversion in `codex/usage.py`.
- [ ] Put untrusted auth, JWT, refresh, and usage shapes in
      `codex/schemas.py` by moving current parsing unchanged; strict validator
      adoption and changed failures belong exclusively to CS-17A.
- [ ] Put the concrete heartbeat adapter under Codex ownership.
- [ ] Keep the user's active Codex login read-only during saved-account
      maintenance.
- [ ] Build the explicit provider registry at composition; remove dynamic or
      unused lookup surfaces.
- [ ] Delete `heartbeat/registry.py`, `heartbeat/claude.py`, and
      `heartbeat/codex.py` only after their ownership moves completely.
- [ ] Keep package initializers thin and avoid private parser re-exports.

**Load-bearing tests:**

- Existing auth detection and refresh behavior remains characterized through
  public boundaries; semantic corrections land in CS-17A.
- JWT claims, identity, plan, and expiry preserve current public behavior.
- Usage, refresh, and heartbeat requests retain current provider behavior.
- Import and export preserve identity and never overwrite the active login.
- Private account copies receive protective permissions.
- External and Sidekick-owned homes remain distinct.
- The explicit registry returns the two composed providers without a dynamic
  plugin mechanism.

**Acceptance:** Codex code imports no CLI, Typer, Rich renderer, or selected
transport/retry library. Persistence imports no provider package. The wheel
contains `providers/codex/` and no stale `codex.py` or concrete heartbeat
adapter modules.

**Recovery:** Revert the complete Codex package and registry move. Do not run
native relocation in this task.

**Commit:** `refactor(codex): create provider-owned integration package`

### 8.23 CS-17A — Normalize provider validation, refresh, and safe failures

**Dependencies:** Recorded CS-07 GO, CS-16, and CS-17.

**Files:**

- Modify Claude and Codex provider, credential/auth, usage, and schema modules.
- Modify the shared provider contract and errors.
- Modify usage and maintenance consumers of provider refresh.
- Modify every current flat-CLI consumer of credential detection, refresh, and
  setup-token before CS-18A.
- Modify provider-focused tests.

**Contract checkpoint:**

- [ ] Select one typed refresh result consumed by usage, maintenance, and
      credential coordination. Success carries the refreshed credentials or
      explicit update; failures remain typed outcomes or errors. Boolean plus
      hidden mutation is forbidden.
- [ ] Define provider-safe error fields that can cross into persistence,
      doctor, maintenance, and presentation without raw payloads, tokens, or
      full identities.
- [ ] Confirm Claude setup-token as a narrow Claude facade/capability and
      remove `run_setup_token()` from the generic provider contract.
- [ ] Update the current `setup-token <provider>` command to invoke the narrow
      Claude facade and return the existing typed unsupported result for every
      other provider; never rely on a missing generic method.

**Work:**

- [ ] Apply the approved validator to Claude and Codex credential, auth, JWT,
      refresh, and usage payloads at their owning boundaries.
- [ ] Distinguish missing, unreadable, malformed, incomplete, expired,
      rejected, and identity-mismatched provider state.
- [ ] Normalize all provider-native expiry units before core.
- [ ] Translate provider/schema failures into safe typed application outcomes
      before any persistence or presentation boundary.
- [ ] Preserve provider-specific diagnostic detail only when it is redacted
      and actionable.
- [ ] Remove unchecked casts and permissive defaults at provider boundaries.
- [ ] Update usage and maintenance to consume the typed refresh contract with
      no Boolean compatibility shim.
- [ ] Update current add, refresh, Codex login/export, and related CLI paths to
      consume typed detection/error states without temporary compatibility
      unions or truthiness fallbacks.

**Load-bearing tests:**

- Claude and Codex missing, unreadable, malformed, incomplete, expired, and
  rejected inputs produce distinct typed states.
- Refresh success carries the intended updated credentials without hidden
  mutation; failure cannot appear as false/absence.
- Malformed provider expiry becomes typed invalid state, never epoch or zero
  remaining.
- A synthetic raw token, credential body, and full provider identity never
  appears in the typed provider outcome or error representation.
- Core and provider APIs expose no selected validation-framework type.
- Claude setup-token remains provider-specific while terminal input stays
  outside the provider package.
- Before the CLI package move, `setup-token claude` still delegates correctly
  and `setup-token codex` still produces the typed unsupported outcome rather
  than an attribute or type error.
- Existing flat add, refresh, Codex login/export, usage, and maintenance paths
  compile and preserve their public outcomes through the typed provider
  contract before credential-service extraction.

**Acceptance:** Provider packages own parsing and safe error translation.
Their public contract is typed, secret-safe, and ready for application-service
coordination.

**Recovery:** Semantic code-only revert; package ownership remains intact.

**Commit:** `refactor(providers): type refresh and validation outcomes`

### 8.24 CS-18 — Introduce provider-neutral credential coordination

**Dependencies:** CS-14 and CS-17A.

**Files:**

- Create `src/sidekick_usages/credentials/__init__.py`.
- Create `src/sidekick_usages/credentials/service.py`.
- Modify provider facades and the current CLI commands.
- Split credential cases out of `tests/test_cli_refresh.py` only when the
  resulting service and command tests are more cohesive.

**Work:**

- [ ] Move credential detection, identity matching, application, refresh,
      import, export, and persistence coordination out of the CLI.
- [ ] Define typed outcomes for missing, unreadable, malformed, incomplete,
      expired, rejected, identity mismatch, and unsupported states.
- [ ] Preserve explicit `replace_identity`; never infer consent from a token
      or local login.
- [ ] Keep Claude and Codex files, schemas, subprocess calls, and protocol
      details inside provider packages.
- [ ] Keep prompts, confirmations, and token input in CLI adapters.
- [ ] Persist one complete account update or none.
- [ ] Preserve diagnostic state without converting rejection into Boolean
      false or generic absence.
- [ ] Sanitize provider failures before persistence, doctor, maintenance,
      human output, or JSON output; never persist a raw token, response body,
      or full provider identity as an error.
- [ ] Keep the active provider login unchanged during saved-account work.

**Load-bearing tests:**

- Detected credentials update the same provider identity and persist once.
- Identity mismatch fails closed and leaves account and active login unchanged.
- Explicit replacement succeeds and records the new identity.
- Missing and malformed sources produce different typed guidance.
- Provider rejection remains an authentication outcome.
- Codex import/export delegates to auth ownership and preserves active login.
- A persistence failure leaves no claimed successful credential update.
- CLI prompts select service inputs but do not implement provider parsing.
- One synthetic-secret propagation test crosses provider, credential service,
  persistence, doctor/maintenance, and human/JSON error channels and proves
  raw token, body, and full identity values are absent everywhere.

**Acceptance:** One canonical credential model and one provider-neutral
coordination service exist. No command reaches provider-private parsers.

**Recovery:** Code-only revert; no schema or path move belongs here.

**Commit:** `refactor(credentials): centralize credential state`

### 8.25 CS-18A — Convert the CLI package and final composition root

**Dependencies:** CS-15, CS-17A, and CS-18.

This change is an atomic same-name module-to-package conversion, but it is
public-command preserving. Provider command groups and deprecation behavior
land separately in CS-20.

**Files:**

- Create `src/sidekick_usages/cli/__init__.py`.
- Create `src/sidekick_usages/cli/app.py`.
- Create `src/sidekick_usages/cli/context.py`.
- Move `cli_help.py` to `cli/help.py`.
- Move `token_input.py` to `cli/token_input.py`.
- Create the approved provider-neutral `cli/commands/` modules.
- Create current-surface Claude and Codex command adapters.
- Delete `src/sidekick_usages/cli.py` atomically.
- Update `src/sidekick_usages/__main__.py`.
- Update `AGENTS.md` in the same commit so contributor structure stays true.
- Update CLI, help, entry-point, and packaging tests.

**Current command ownership:**

| Command module | Registration and option owner |
|---|---|
| `usage.py` | default invocation and `check` |
| `accounts.py` | `list`, `remove`, `rename`, `set-plan`, `reset` |
| `credentials.py` | `add` and the sole `refresh` registration/parser |
| `heartbeat.py` | group, label fallback, enable, disable, status |
| `maintenance.py` | `maintain` and maintenance result presentation |
| `doctor.py` | `doctor` and diagnostic presentation |
| `daemon.py` | install, status, uninstall |
| `updates.py` | `check-update`, `update` |
| `claude.py` | current top-level `setup-token` adapter |
| `codex.py` | current `codex-login` and `codex-export` adapters |

`refresh --all` remains parsed and registered only by `credentials.py`; that
branch delegates to the maintenance service and its outcome presentation.
There is no second refresh command or duplicated option validation.

**Work:**

- [ ] Make `cli/__init__.py` re-export only `app` and `run`.
- [ ] Make `create_app()` registration-only with explicit registration
      functions and no dynamic discovery or global-app imports.
- [ ] Build an invocation-scoped `AppContext` from services, a proven direct
      account-store need, consoles, and the narrow Claude setup-token facade.
- [ ] Keep raw paths, clocks, HTTP, provider registries, scheduler backends,
      flags, collected results, and failures out of `AppContext`.
- [ ] Keep token prompts in `cli/token_input.py`; delegate setup-token execution
      through the narrow Claude facade, never provider internals or a registry.
- [ ] Define a separate typed doctor composition result: a ready diagnostic
      service state or a migration-blocked assessment state. Do not use broad
      optional fields or a service locator.
- [ ] Let the doctor command render blocked schema/corruption recovery without
      constructing `AccountStore`.
- [ ] Own resources transactionally with `ExitStack` or equivalent; transfer
      ownership only after composition succeeds and close on partial failure.
- [ ] Use Typer/Click `ctx.obj`; remove the module singleton and `set_context`.
- [ ] Keep help and version on a no-composition path.
- [ ] Move complete command clusters while preserving every current command,
      option, help entry, output channel, and exit contract.
- [ ] Preserve `sidekick_usages.cli:app` and `python -m sidekick_usages`.
- [ ] Keep `cli/app.py` near or below 200 lines and command modules below 800.

**Load-bearing tests:**

- Root, nested, and leaf help discover the current command surface without
  path discovery, migration assessment, store, credentials, scheduler, or
  HTTP construction.
- Version remains one undecorated line without composition.
- Default invocation and `check` use the same usage service.
- `refresh` has one registration; its `--all` path delegates to maintenance.
- Setup-token input stays in CLI and execution uses the narrow Claude facade.
- Schema-corrupt and migration-blocked doctor output works while
  `AccountStore` construction is never attempted.
- A typed failure after HTTP-pool creation closes every acquired resource once
  and preserves the original error.
- An explicitly empty provider registry remains empty through composition.
- JSON, quiet, scheduled, and current human output remain stable.
- Source and wheel contain `cli/` and no stale `cli.py` or `cli_help.py`.
- Source and isolated-wheel entry points both work.

**Recovery:** Revert the complete package conversion. No provider hierarchy,
deprecation, dependency adoption, or data migration belongs here.

**Commit:** `refactor(cli): create command packages and lazy composition`

### 8.26 CS-19 — Activate native application-data migration conditionally

**Dependencies:** Recorded operator CS-09 GO, CS-10, CS-14, CS-17, CS-17A,
CS-18, and CS-18A.

If CS-09 or any migration gate is NO-GO, record the outcome, retain
compatibility paths, omit the runtime dependency, and continue with CS-20.
Do not define `PrivateAuthMigrator`, adapter conformance, or migration-only
tests under a NO-GO disposition.

**Files when GO:**

- Modify `src/sidekick_usages/paths.py`.
- Modify `src/sidekick_usages/persistence/migrations.py`.
- Modify `src/sidekick_usages/providers/codex/auth.py` only for the approved
  port implementation.
- Modify `src/sidekick_usages/doctor.py` for read-only assessment and guidance.
- Modify final composition in `src/sidekick_usages/cli/app.py` and the typed
  doctor composition path.
- Modify `pyproject.toml` and `uv.lock` to add the approved direct
  `platformdirs` dependency.
- Add focused path/location migration tests and doctor cases.

**Required internal order:**

- [ ] Freeze exact canonical paths from CS-09 evidence.
- [ ] Implement side-effect-free, generation-aware read-only assessment.
- [ ] Define the narrow `PrivateAuthMigrator` port in persistence and implement
      it in Codex auth in this conditional change set only.
- [ ] Make normal executable composition assess locations before store load.
- [ ] Allow `doctor` to compose and render assessment without a loaded store.
- [ ] Inject the Codex `PrivateAuthMigrator` from composition.
- [ ] Prove resolved containment below the existing private root; never use a
      string-prefix test.
- [ ] Reject symlink escapes, misleading shared prefixes, external homes,
      collisions, and partial destinations.
- [ ] Prove idempotence and concurrency behavior before enabling writes.
- [ ] Copy and validate every required private auth bundle.
- [ ] Preserve account and auth permissions.
- [ ] Atomically commit rewritten account state last.
- [ ] Retain every old durable source and backup; delete nothing automatically.
- [ ] Treat lifetime cache independently and regenerate it only after its
      lifecycle is proven.
- [ ] Repeat the CS-14 compatibility harness after native relocation: make a
      new canonical write, run the approved rollback preparation into the
      compatibility generation, and prove the actual `v0.6.0` binary reads the
      latest representable state.

**Generation matrix:**

- No account candidate produces documented empty initial state.
- Prototype only imports once and remains untouched.
- Existing Sidekick only migrates to absent canonical state.
- Canonical only proceeds without compatibility fallback.
- Equivalent canonical and existing stores reconcile explicitly.
- Conflicting authoritative stores fail closed.
- Malformed or unreadable authoritative state fails closed.
- Prototype corruption fails only when it is the sole candidate.
- Stale prototype state does not block or overwrite authoritative state.

**Private-auth matrix:**

- Equal old and canonical roots require no relocation.
- Distinct roots preserve each account's relative destination.
- Existing-root descendants migrate.
- External and provider-native homes remain untouched.
- Already-canonical homes remain canonical.
- Misleading sibling prefixes and symlink escapes are rejected.
- Equivalent destinations are idempotent; conflicting or partial destinations
  are typed failures.
- Account paths change only after all required copies validate.

**Operational acceptance:**

- `doctor` reports selected generation, source, destination, conflict or
  partial state, backup, and recovery action without secrets.
- Non-diagnostic commands stop on conflict or partial state.
- Help and version bypass discovery and assessment.
- Linux, macOS, Windows, and WSL behavior passes the approved platform matrix.
- Persistence remains the sole coordinator and imports no provider package.
- Only `paths.py` imports `platformdirs`.
- Review-branch commits require focused local gates; merge/release activation
  requires Linux, macOS, Windows, WSL, and rollback evidence recorded in the
  final parity record.

**Recovery:** Use the documented CS-10 rollback preparation to preserve latest
representable writes in the compatibility generation. A retained old snapshot
is an emergency source, not a lossless post-write rollback. Do not recover by
deleting the newer file or copying data blindly. A code revert alone is
insufficient after new writes.

**Commit:** `feat(persistence): migrate native application data safely`

### 8.27 CS-20 — Add provider command groups and deprecation lifecycle

**Dependencies:** CS-18A and CS-19, whether CS-19 is GO or recorded NO-GO.

**Files:**

- Modify `cli/app.py` registration.
- Modify `cli/commands/claude.py` and `cli/commands/codex.py`.
- Modify provider command/help tests.
- Modify README and the tracked release/deprecation contract required for
  release `R`; do not edit the Release Please-owned changelog directly.

**New hierarchy:**

```text
sidekick-usages claude setup-token
sidekick-usages codex login
sidekick-usages codex export
```

The current top-level spellings remain thin deprecated delegates for the
approved release window. They contain no copied command workflow.

**Work:**

- [ ] Record actual release `R` and alias-removal versions as required by
      section 5.2.
- [ ] Register explicit Claude and Codex groups without dynamic discovery.
- [ ] Delegate Claude setup-token through the existing narrow facade and keep
      terminal input in the CLI.
- [ ] Delegate Codex login/export through the same credential service and auth
      adapter used by the existing commands.
- [ ] Keep current top-level spellings as thin aliases through the approved
      versions and remove no alias early.
- [ ] Emit deprecation only on human stderr/help, never machine stdout.
- [ ] Update live help, README, and the design release contract with canonical
      commands and exact removal versions.
- [ ] Use a Conventional Commit subject and PR title/body that name both the
      new hierarchy and deprecated aliases so Release Please has accurate
      release-note input.

**Load-bearing tests:**

- Root and provider-group help discover the new hierarchy and marked aliases.
- Canonical and alias provider commands produce the same service outcome.
- Alias deprecation appears only in approved human channels.
- Setup-token input remains in CLI while the Claude facade executes it.
- Codex canonical and alias commands preserve active-login safety.
- JSON, quiet, scheduled, and version output receive no deprecation text.
- Help/version remain free of runtime composition after group registration.

Do not implement aliases as copied commands or reintroduce a provider registry
into command-facing context.

**Recovery:** Revert the hierarchy/alias feature while leaving the CLI package
and services intact.

**Commit:** `feat(cli): add provider commands with deprecated aliases`

### 8.28 CS-21 — Finalize presentation ownership without redesign

**Dependencies:** CS-15, CS-15A, CS-16, CS-17, CS-17A, CS-18, CS-18A,
CS-19, and CS-20.

**Files:**

- Move `src/sidekick_usages/render.py` to
  `src/sidekick_usages/usage/render.py` atomically.
- Modify heartbeat rendering only for typed model imports and shared outcomes.
- Finalize doctor presentation in the CLI adapter.
- Modify JSON serialization at the owning command/presentation boundaries.
- Update top-level renderer path references in `docs/debugging-claude.md` in
  the same commit.
- Refocus rendering tests without duplicating the approved TUI specification.

**Work:**

- [ ] Make human builders return Rich renderables and never print.
- [ ] Make existing doctor and heartbeat machine builders return typed JSON
      values under one convention. Do not invent usage-check JSON output.
- [ ] Keep commands as the only stdout/stderr and exit-code owners.
- [ ] Pass explicit reference time or completed display values to renderers.
- [ ] Keep provider, persistence, filesystem, network, and clock acquisition
      out of renderers.
- [ ] Consolidate refresh/outcome rendering only where at least three current
      callers share the same semantics.
- [ ] Preserve branding as the one robot and product-copy source.
- [ ] Preserve the approved provider-panel counts, 85-column floor, narrow
      fallback, help masthead, and all machine-output exclusions.
- [ ] Do not split the 739-line usage renderer mechanically unless it crosses
      800 lines with a concrete second responsibility.

**Load-bearing tests:**

- Provider panels retain singular/plural account counts and approved layout.
- The overview renders without wrapping at the approved floor and deliberately
  degrades below it.
- One canonical robot source remains.
- Human timestamps are deterministic under an explicit reference time.
- Existing doctor and heartbeat human/JSON views represent the same typed
  results without UI leakage; usage check remains human-only.
- Quiet and scheduled modes remain undecorated.
- Renderer imports and fakes prove no I/O or time acquisition occurs.

**Acceptance:** This task is an ownership move and contract cleanup, not a TUI
redesign. Exact approved visual tests remain load-bearing.

**Recovery:** Atomic renderer/package move revert.

**Commit:** `refactor(render): enforce presentation boundaries`

### 8.29 CS-22 — Enforce architecture hygiene and prune test slop

**Dependencies:** CS-21.

**Files:**

- Modify `pyproject.toml`, `.pre-commit-config.yaml`, and CI only for clean
  enforceable rules.
- Modify `.github/workflows/ci.yml` to run an installed-wheel smoke across
  Linux, macOS, and Windows.
- Modify the CS-11 `packaging/smoke_wheel.py` verifier for final package
  contracts and use it from local gates, CI, and release verification.
- Add one cohesive architecture test or script.
- Modify tests whose ownership or value changed.

**Build-versus-adopt checkpoint:**

- [ ] Compare existing Ruff/TID rules, a focused standard-library AST check,
      and an actively maintained import-boundary tool.
- [ ] Prefer existing tools when they express the rules clearly.
- [ ] Use one small AST-based repository check when custom rules are simpler
      than another dev dependency.
- [ ] Do not add a library from reputation alone or create one pytest per
      prohibited string.

**Work:**

- [ ] Eliminate remaining production and test `Any` and unjustified casts.
- [ ] Replace JSON `Any` with recursive JSON types or boundary schemas.
- [ ] Type pytest fixtures, monkeypatches, fakes, and helper return values.
- [ ] Verify native Python 3.14 deferred annotations consistently and reject
      the legacy stringizing future import.
- [ ] Complete design sections 3.3 and 3.7 conformance: PEP 695 for new aliases
      and generics, explicit public signatures and optional state,
      standard-library enums/types, and concise Sphinx fields.
- [ ] Enable focused annotation enforcement from a clean baseline.
- [ ] Enforce the 1000-line hard limit and emit an approximately 800-line
      review warning.
- [ ] Enforce core, service, provider, HTTP, persistence, CLI, path, clock, and
      renderer dependency directions from design section 12.
- [ ] Consume every architecture check in design section 16.5 rather than a
      hand-selected subset.
- [ ] Enforce no production `timestamps.py` or universal timestamp formatter.
- [ ] Enforce no paths, clocks, raw HTTP, provider registry, or scheduler
      backend in `AppContext`.
- [ ] Enforce no import-time Sidekick path discovery or duplicate
      Sidekick-owned `Path.home()` reconstruction.
- [ ] Enforce no private-Codex root reconstruction outside `paths.py`.
- [ ] Enforce no clock import from `core/expiry.py`, and no direct current-time
      acquisition in services, providers, or renderers.
- [ ] Enforce no provider/persistence timestamp-serializer cross-import.
- [ ] Enforce `HTTPMethod` request construction and no selected-library type
      leakage from `http/`.
- [ ] Enforce no application-wide settings singleton or unapproved
      `pydantic-settings` dependency.
- [ ] Enforce one HTTP retry owner, one Sidekick path owner, one migration
      coordinator, and one robot source.
- [ ] Under a recorded CS-09 NO-GO, enforce absence of `platformdirs`,
      `PrivateAuthMigrator`, and native-migration-only production code.
- [ ] Enforce no stale same-named module after package conversions.
- [ ] Remove blanket or unjustified suppressions and dead gate configuration.
- [ ] Remove the inert CLI-app existence smoke while retaining the package
      version smoke.
- [ ] Remove private-helper tests replaced by service tests.
- [ ] Remove redundant exact-and-order assertions and duplicated render
      fixtures only when the remaining test protects the same behavior.
- [ ] Introduce one small explicit typed `AppContext` test harness only if
      three or more final command modules still repeat it.
- [ ] Build the wheel once per CI run or share a verified artifact, install it
      into an isolated environment on every supported OS, and exercise both
      the console script and `python -m sidekick_usages` through that
      environment's explicit interpreter.
- [ ] Make `packaging/smoke_wheel.py` require exactly one wheel from its fresh
      test-owned build output, inspect ZIP members for required packages and
      forbidden stale modules, create an isolated environment, install that
      exact wheel, clear source leakage, run outside the checkout, and invoke
      both entry paths.

**Acceptance:**

- Every architecture check fails against a deliberate local violation and
  passes the real tree.
- No production module exceeds 1000 lines.
- No production or test `Any`, unjustified cast, blanket suppression, or
  swallowed exception remains.
- Tests are fewer where redundant and stronger where behavior matters.
- Ruff, `ty`, pytest, pre-commit, Markdown, and build are all green.
- Linux, macOS, and Windows CI prove the installed wheel rather than only the
  editable checkout; no Unix-only `bin/` path is used on Windows.

**Recovery:** Revert each mechanical rule with the code cleanup that made it
green; never keep a rule disabled by blanket suppression.

**Commit:** `chore(quality): enforce architecture hygiene gates`

### 8.30 CS-23 — Complete documentation, packaging, recovery, and parity proof

**Dependencies:** CS-22 and any GO native-migration workstream.

**Files:**

- Modify `AGENTS.md` and `README.md`.
- Modify `docs/token-maintenance.md`, `docs/heartbeat.md`, and
  `docs/debugging-claude.md` where behavior or paths changed.
- Modify Homebrew documentation/generator tests for dependency changes.
- Modify the design authority with final dependency selections and measured
  implementation evidence.
- Record final plan execution and parity status in this file or a tracked
  completion record.

**Work:**

- [ ] Update documented package structure and ownership.
- [ ] Document canonical provider commands, legacy aliases, release `R`, and
      removal versions.
- [ ] Document current/compatibility/native paths and whether CS-19 was GO or
      NO-GO.
- [ ] Document stored-schema backup, doctor guidance, rollback, and recovery.
- [ ] Document HTTP pooling, retry safety, timeout bounds, and error behavior
      without exposing provider secrets.
- [ ] Update maintenance, heartbeat, and debugging commands from live help.
- [ ] Verify runtime dependency metadata, lockfile, wheel, sdist, and Homebrew
      generator behavior.
- [ ] Do not manually edit release versions or regenerate an unreleased
      formula against an older release archive.
- [ ] Let Release Please own version and changelog updates.
- [ ] Verify the generated Release Please changelog entry names the provider
      hierarchy and deprecated aliases; correct release-note inputs rather
      than hand-editing an unreleased changelog. Live help, README, and the
      tracked release contract remain authoritative for exact removal versions.
- [ ] Install the built wheel in an isolated environment and exercise help,
      version, canonical commands, and compatibility aliases.
- [ ] Run the complete supported-platform CI matrix and recorded WSL smoke.
- [ ] Perform the specification-parity sweep in section 10.

**Acceptance:** A new contributor can understand the final tree and a user can
upgrade, diagnose, recover, or roll back without consulting implementation
source. Package artifacts contain no stale modules.

**Recovery:** Documentation/packaging revert if no release occurred. After a
release or migration, follow the documented data and alias lifecycle rather
than assuming a Git revert is sufficient.

**Commit:** `docs(architecture): document completed application migration`

## 9. Explicit non-goals and prohibited shortcuts

The following work is outside this migration unless a new approved design
adds a concrete product requirement:

- dynamic provider plugins or Python entry-point discovery;
- a dependency-injection container or service locator;
- a generic command base class or command registry framework;
- a generic repository, unit-of-work, result-monad, or event-bus layer;
- a generic mapper, catch-all `utils.py`, or project-wide schema module;
- asynchronous HTTP, HTTP/2, a circuit breaker, or arbitrary retry hooks;
- an application-wide settings model or configuration package;
- a universal timestamp parser or formatter;
- moving provider-native homes into `ApplicationPaths`;
- moving scheduler installation paths out of daemon ownership;
- an ORM, database, or account-service process;
- a new TUI visual design;
- forced identical Claude and Codex leaf modules;
- automatic deletion of compatibility data, backups, or old credentials;
- a restore command that has not been designed and threat-modelled; and
- extensibility parameters with one caller.

Prohibited implementation shortcuts include:

- scaffolding the whole target tree with empty files;
- keeping flat and packaged implementations side by side;
- wrapping an old API solely to keep private tests unchanged;
- adding all dependency candidates and deciding later;
- stacking urllib3 retry with Tenacity or a manual loop;
- using `Any`, casts, coercion, or fallback defaults to bypass validation;
- treating malformed credentials, lifetime data, or account state as absent;
- using string prefixes for filesystem containment;
- combining stored-schema and native-location migration;
- mixing user-data changes into module-move commits;
- printing from services or performing I/O from renderers;
- adding one negative-string test per architectural rule;
- enabling a noisy rule and silencing its findings;
- preserving dead exports without an identified supported consumer;
- using real account data for a migration or CLI smoke; and
- claiming platform support from Linux-only tests.

The rule of three still applies. Product richness does not justify speculative
frameworks, and small diffs do not justify an inadequate architecture.

## 10. Specification traceability

### 10.1 Approved decisions

| Approved decision | Implemented by |
|---|---|
| Narrow `core/` and dependency rules | CS-12A, CS-13, CS-22 |
| Provider-owned Claude and Codex packages | CS-16, CS-17 |
| Provider-owned heartbeat adapters | CS-16, CS-17 |
| CLI package and nested commands | CS-18A |
| Help adapter outside commands | CS-18A |
| Concrete Claude and Codex commands | CS-20 |
| Compatibility alias lifecycle | CS-20, CS-23 |
| Boundary-local schemas | CS-07, CS-13, CS-14, CS-17A |
| Validation decision before adoption | CS-07 |
| Entry point and machine behavior | CS-11, CS-15, CS-18A, CS-20, CS-23 |
| Phased, load-bearing migration | Every change set |
| Shared HTTP outside core | CS-08, CS-11 |
| One retry owner and safe POST policy | CS-08, CS-11 |
| Mandatory pooled transport | CS-08, CS-11 |
| Provider-neutral expiry owner | CS-13 |
| Aware wall time and monotonic deadlines | CS-10A, CS-11, CS-13 |
| Boundary-owned timestamp encoding | CS-13, CS-17A, CS-21 |
| Frozen `ApplicationPaths` and injection | CS-12, CS-18A, CS-19 |
| Provider/scheduler path exclusions | CS-12, CS-19, CS-22 |
| Conditional `platformdirs` adoption | CS-09, CS-19 |
| Compatibility centralization before relocation | CS-12, CS-19 |
| No speculative settings framework | CS-07, CS-09, CS-22 |
| Lazy composition and service-facing context | CS-11, CS-18A |
| Generation-aware source precedence | CS-14, CS-19 |
| Sole migration coordinator and injected auth port | CS-14, CS-19 |

### 10.2 Required correctness fixes

| Required correction | Implemented by |
|---|---|
| Unknown daemon input cannot uninstall | CS-01 |
| Store migration fails loudly | CS-10, CS-14 |
| Credential absence and corruption differ | CS-17A, CS-18 |
| Lifetime/expiry failure is not zero | CS-13, CS-13A, CS-17A |
| Explicit empty heartbeat registry remains empty | CS-02, CS-18A |
| Help and masthead share width policy | CS-03, CS-18A |
| Reserved fixture identities | CS-05 |
| Provider-native time units stop at adapters | CS-13, CS-17A |

### 10.3 Target-tree deletions and conversions

The final sweep must prove that these obsolete production paths are gone:

```text
src/sidekick_usages/cli.py
src/sidekick_usages/cli_help.py
src/sidekick_usages/token_input.py
src/sidekick_usages/http.py
src/sidekick_usages/store.py
src/sidekick_usages/report.py
src/sidekick_usages/providers/claude.py
src/sidekick_usages/providers/codex.py
src/sidekick_usages/heartbeat/base.py
src/sidekick_usages/heartbeat/domain.py
src/sidekick_usages/heartbeat/claude.py
src/sidekick_usages/heartbeat/codex.py
src/sidekick_usages/heartbeat/registry.py
src/sidekick_usages/render.py
```

Their approved package or module replacements must exist and be present in the
built wheel. Do not delete a path until every real caller has moved.

## 11. Final specification-parity sweep

CS-23 must finish a review matrix with these columns:

| Authority | Owning change set | Code evidence | Test evidence | Docs | Status |
|---|---|---|---|---|---|

The matrix may live in this plan's completion section or a concise tracked
completion record. It must cover:

1. Every goal and non-goal in design section 2.
2. Every invariant in design section 3.
3. Every target package entry in design section 4.1.
4. Path discovery, precedence, migration, and recovery in section 4.2.
5. Core, schema, provider, CLI, service, HTTP, presentation, and dependency
   contracts in sections 5 through 14.
6. Every migration acceptance criterion in section 15.9.
7. Every test and architecture requirement in section 16.
8. Every final gate in section 17.
9. Every security and operational constraint in section 18.
10. Every approved decision in section 21.
11. The related TUI specification, proving no unauthorized visual change.
12. Every conditional decision, marked implemented or explicitly NO-GO with
    evidence and no dead production dependency.

No item may be marked complete from a commit message or intended architecture.
Each status requires current code, behavior test, documentation, or artifact
evidence appropriate to the claim.

## 12. Final verification

### 12.1 Repository gates

Run from a synchronized environment:

```bash
uv sync --all-groups
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run ty check src/ tests/
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files
npm ci
npm run lint:markdown
uv build
git diff --check
git status --short
```

Do not use aggregate success as a substitute for focused architecture checks.

### 12.2 Focused source checks

Confirm:

- no production module exceeds 1000 lines;
- every module near 800 lines received a cohesion review;
- no unauthorized `Any`, cast, blanket suppression, or swallowed exception
  exists in source or tests;
- no direct current-time acquisition remains in services, providers, or
  renderers outside the approved clock owner;
- no duplicate Sidekick-owned root reconstruction exists;
- no transport or retry-library import exists outside `http/`;
- no `platformdirs` import exists outside `paths.py` when CS-19 is GO;
- no `pydantic-settings` dependency or production import exists;
- core has no infrastructure dependency;
- persistence has no provider import;
- services have no Rich or Typer import;
- renderers have no provider, persistence, filesystem, network, or clock
  acquisition;
- package initializers remain thin;
- exactly one robot source remains; and
- all removed flat modules are absent.

### 12.3 Artifact and CLI checks

Build and verify the exact artifact with the tracked cross-platform verifier:

```bash
uv run python packaging/smoke_wheel.py --build
```

The verifier creates a fresh test-owned build directory, runs `uv build` into
it, fails unless that build produced exactly one wheel, and ignores unrelated
artifacts already under `dist/`. It inspects that ZIP, creates an isolated
environment, installs that exact path, removes working-tree import leakage,
changes to a test-owned directory, and invokes the installed console script
plus that environment's explicit interpreter. It runs version, root help,
representative nested help, provider-group help, and
`python -m sidekick_usages --version`. This same command runs in the Linux,
macOS, and Windows CI matrix.

Use synthetic/test-owned state to verify:

- default and explicit check;
- existing doctor and heartbeat JSON parsing;
- quiet and scheduler-safe output;
- account CRUD and durable persistence;
- canonical and deprecated provider commands;
- doctor on healthy, corrupt, conflict, partial, and recovery states;
- old-schema upgrade and documented rollback;
- native-location upgrade when CS-19 is GO; and
- active Claude and Codex logins remain untouched.

Do not exercise a real account or user credential during acceptance.

### 12.4 Platform and release checks

- Run the full Python 3.14 test matrix on Linux, macOS, and Windows.
- Run a recorded WSL path, daemon, wheel, and CLI smoke.
- Verify TLS, CA, proxy, timeout, pool, and retry behavior on supported
  platforms after the HTTP change.
- Verify account and credential permissions on each platform's supported
  model.
- Verify the Homebrew generator includes every direct runtime dependency.
- Let the release workflow build the formula against the actual new tag.
- Verify the resulting Homebrew/tap change installs and passes its tests.
- Confirm Release Please version and changelog state; do not manually bump.
- Refresh the current package, Commitizen, and private Node metadata and fix
  only proven release-source drift before publishing.

### 12.5 Completion definition

The migration is complete only when:

- the design, plan, research decisions, and completion evidence are tracked;
- all non-conditional change sets are implemented;
- CS-19 is either fully implemented or recorded as a clean NO-GO with no
  unused dependency or dead production branch;
- the specification-parity matrix has no unexplained gap;
- every final repository, platform, package, security, and recovery gate is
  green;
- no real credential, account export, or full provider identity entered Git;
- the current release can upgrade safely and follow documented rollback;
- command aliases have exact release dates and removal versions; and
- the final worktree contains no untracked normative document.

## 13. Recommended commit sequence

The review sequence is:

1. `docs(architecture): add maintainable application plan`
2. `fix(cli): make daemon operations exhaustive`
3. `fix(heartbeat): preserve explicit empty registries`
4. `fix(help): share terminal width policy`
5. `chore: remove dead surfaces and stale comments`
6. `docs: clean identities and markdown gates`
7. `chore(quality): enable clean hygiene gates`
8. `docs(research): decide schema validation dependency`
9. `docs(research): decide HTTP transport and retry dependency`
10. `docs(research): decide application path discovery dependency`
11. `docs(persistence): define schema migration recovery contract`
12. `refactor(time): inject explicit application wall time`
13. `refactor(http): centralize transport and retry policy`
14. `refactor(paths): inject current sidekick-owned paths`
15. `refactor(core): centralize proven shared types`
16. `refactor(core): normalize models and expiry policy`
17. `fix(lifetime): preserve collection failure states`
18. `refactor(store): validate and persist accounts explicitly`
19. `refactor(usage): extract typed usage check service`
20. `refactor(heartbeat): name models and ports explicitly`
21. `refactor(claude): create provider-owned integration package`
22. `refactor(codex): create provider-owned integration package`
23. `refactor(providers): type refresh and validation outcomes`
24. `refactor(credentials): centralize credential state`
25. `refactor(cli): create command packages and lazy composition`
26. `feat(persistence): migrate native application data safely`, only on GO
27. `feat(cli): add provider commands with deprecated aliases`
28. `refactor(render): enforce presentation boundaries`
29. `chore(quality): enforce architecture hygiene gates`
30. `docs(architecture): document completed application migration`

Split a change set only when both resulting commits are independently
production-valid and the plan is updated first. Never split the same-named
module-to-package conversions or persistent transaction boundaries across
commits. Never squash unrelated user-data, dependency, presentation, and
mechanical-gate changes into one review.

[publication-ci]: https://github.com/Sawmonabo/sidekick-usages/actions/runs/29064704915
[cs01-publication-ci]: https://github.com/Sawmonabo/sidekick-usages/actions/runs/29065786820
[typer-0268-release]: https://typer.tiangolo.com/release-notes/#0268
[typer-0268-rich-width]: https://github.com/fastapi/typer/blob/0.26.8/typer/rich_utils.py
[prettier-prose-wrap]: https://prettier.io/docs/options.html#prose-wrap
