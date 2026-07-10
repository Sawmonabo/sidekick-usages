# Maintainable application architecture completion record

## Scope and authority

This record closes the implementation authorized by:

- `docs/superpowers/specs/2026-07-09-maintainable-application-architecture-design.md`;
- `docs/superpowers/plans/2026-07-09-maintainable-application-architecture.md`;
  and
- the approved usage-TUI design, which remains the visual authority.

The implementation branch is `develop`. The production implementation and
behavior tests are anchored by commit `98de5fa2b3f1c0a019de03cc7aabac79e9ef6292`,
with cross-platform fixture corrections in commits `6d78c95` and `e2a2513`.
The released compatibility baseline is `v0.6.0` at commit
`6a413b2772c3c11e9ef45a78a06ab79bfc0ca44c`.

This record cites tracked source, tests, documentation, built artifacts, and
live gate results. It does not treat an intended design or commit subject as
proof of behavior.

## Conditional decision closure

| Decision | Disposition | Implemented evidence | Reversal condition |
| --- | --- | --- | --- |
| CS-07 boundary validation | GO: Pydantic 2.13.4 | `persistence/*schema*`, provider `schemas.py`, and `serialization/json.py` are the only approved Pydantic owners; `pyproject.toml` and `uv.lock` pin the selected version. | Reopen if Python 3.14, source-build, security, or boundary-validation support regresses. |
| CS-08 HTTP transport and retry | GO: urllib3 2.7.0 plus one Sidekick retry executor | `http/client.py` owns pooled transport; `http/retry.py` owns the closed retry policy; no other production package imports a transport or retry library. | Reopen if platform TLS/proxy support, security, lifecycle, or retry correctness regresses. |
| CS-09 native application paths | GO: platformdirs 4.10.0 | `paths.py` is the only importer; `migrate locations` is the sole explicit relocation surface; compatibility data is never relocated at startup. | Reopen if native discovery, permissions, rollback, supported-platform packaging, or WSL behavior regresses. |
| Settings framework | NO-GO | No `pydantic-settings`, global settings model, `settings.py`, or configuration package exists. Policy remains with its behavioral owner. | Revisit only for a concrete multi-source settings contract with fields, precedence, and multiple consumers. |
| Tenacity or stacked retry engines | NO-GO | The selected urllib3 transport has library retries disabled and delegates to the single focused executor. | Revisit only if a new concrete operation cannot be expressed safely by the closed policy. |
| Hidden native relocation | NO-GO | Runtime assessment selects compatibility state but never writes it; only `migrate locations [--yes]` can relocate an existing generation. | No reversal without a separately approved migration contract. |

The detailed build-versus-adopt evidence remains tracked under
`docs/superpowers/research/`. No temporary or ignored path is a normative
dependency of the implementation.

## Verification evidence

### Local repository and artifact gates

The synchronized Python 3.14.6 environment passed:

- the repository architecture checker, with no violation;
- Ruff lint and format checks;
- `ty` over production and tests;
- the complete pytest suite: 784 passed, four native-platform cases skipped on
  WSL, and 72 percent branch coverage;
- every pre-commit hook, including Bandit, `uv-secure`, Commitizen, and the
  architecture contract;
- Markdown lint with `markdownlint-cli2` 0.23.0;
- `npm audit --audit-level=moderate` with zero vulnerabilities;
- wheel and source-distribution construction; and
- `git diff --check`.

`packaging/smoke_wheel.py --build` built exactly one wheel in a fresh output
directory, inspected its members, installed that exact wheel into an isolated
environment, cleared source-tree import leakage, changed outside the checkout,
and exercised both the console script and `python -m sidekick_usages`. It
verified root, nested, canonical provider, and deprecated-alias help without
using real account state.

The Node audit initially found the
[js-yaml alias-expansion advisory](https://github.com/advisories/GHSA-h67p-54hq-rp68)
and the
[markdown-it smartquotes advisory](https://github.com/advisories/GHSA-6v5v-wf23-fmfq)
through `markdownlint-cli2` 0.22.1. Updating the one direct development
dependency to 0.23.0 selected fixed parser releases; a clean install and audit
then reported zero vulnerabilities.

The released compatibility harness passed its focused tests and two complete
cycles. Each cycle upgraded released generation-zero data, prepared exact
rollback bytes, executed the pinned released reader, and upgraded again. It
also rejected the one deliberately unrepresentable empty-heartbeat downgrade
before mutation.

### Release automation

Release Please remains the sole version and changelog owner. Commitizen now
validates conventional commit messages only; its stale independent bump and
changelog configuration is gone. The private Node tooling package has no
product version mirror, declares Node 22 or newer, and is not publishable.

A read-only Release Please 17.6.1 dry run against pushed `develop` and the
tracked manifest proposed `0.7.0`. Its first Added entry was:

```text
cli: add provider hierarchy and deprecated aliases
```

The preview selected released `v0.6.0` as its base and proposed the tracked
Python version, package version, manifest, and changelog updates. No release
file or changelog was edited manually.

The tooling decision is grounded in the current primary contracts:

- [Release Please CLI documentation](https://github.com/googleapis/release-please/blob/main/docs/cli.md)
  defines manifest-driven `release-pr` and the non-mutating `--dry-run` mode.
- [Commitizen bump documentation](https://commitizen-tools.github.io/commitizen/commands/bump/)
  confirms that its version fields drive independent version-file mutation.
- [Commitizen changelog documentation](https://commitizen-tools.github.io/commitizen/commands/changelog/)
  confirms that `update_changelog_on_bump` is an independent changelog writer.
- [npm package metadata documentation](https://docs.npmjs.com/files/package.json/)
  makes versions optional for non-published tooling packages and defines the
  `engines` contract.
- [setup-node's canonical documentation](https://github.com/actions/setup-node)
  recommends an explicit Node version and supports the v6 npm cache used by
  CI.

### Platform evidence

- [GitHub Actions run 29112534600](https://github.com/Sawmonabo/sidekick-usages/actions/runs/29112534600)
  passed the complete implementation matrix on `develop`.
- The CI workflow runs Python 3.14 tests and exact installed-wheel smoke on
  Linux, macOS, and Windows.
- The pre-commit job pins Node.js 22, performs a clean npm install, fails on a
  moderate-or-higher audit finding, and lints every tracked Markdown document.
- The workflow runs the pinned released-v0.6 compatibility harness on Linux.
- The Homebrew source-build job resolves the generated formula, installs it,
  and runs `brew test` on Linux and macOS.
- The Homebrew dependency test proves the locked host closure includes Click,
  platformdirs, Portalocker, Pydantic, Rich, Typer, and urllib3, and excludes
  the Windows-only pywin32 dependency on non-Windows hosts.
- A WSL2 Linux 6.6/ext4 smoke with Python 3.14.6 proved absolute XDG account,
  private-auth, and cache discovery without creating a directory or file.
- The same WSL smoke executed version and both provider help paths and observed
  the installed Windows Task Scheduler integration in `Ready` state. Its
  pre-existing last-result value was treated as historical operator state;
  acceptance did not run maintenance against real accounts.

## Cohesion review for warning-sized modules

The architecture gate fails above 1000 lines and warns at 800. Every warning
was reviewed rather than silenced.

| Module | Lines | Review result |
| --- | ---: | --- |
| `persistence/filesystem.py` | 838 | Retain. It is one qualified authority-file adapter implementing read, immutable publication, compare-and-replace, and uncertain-replacement classification under one filesystem identity. Extracting its private publication steps would expose transaction-coupled internals without a second owner. |
| `persistence/migrations/account.py` | 809 | Retain. It is one schema/prototype/rollback coordinator with its narrow lock and released-reader ports. Its paths share one assessment and recovery state machine; native location migration already has a separate coordinator. |
| `tests/test_cli_refresh.py` | 989 | Retain. The cases protect distinct add, refresh, login, export, identity-replacement, and setup-token command outcomes through one intentionally shared provider/context harness. No assertion-only or existence smoke remains. |
| `tests/test_credential_service.py` | 907 | Retain. The cases protect source-state distinctions, identity proof, transactional private bundles, export ordering, and secret-safe failures through one service harness. |
| `tests/test_heartbeat.py` | 911 | Retain. The file exercises model invariants, service decisions, human/JSON/quiet command behavior, provider target selection, and maintenance ordering with shared typed fakes. |
| `tests/test_persistence_coordinator.py` | 989 | Retain. Its in-memory transaction fixture drives schema migration, prototype import, rollback, scheduler blocking, reset ordering, and checkpoint recovery without private-helper assertions. |
| `tests/test_persistence_credential_transactions.py` | 826 | Retain. Its crash adapters exercise authority-before/after boundaries, multi-bundle convergence, source guards, bundle cleanup, and malformed-journal closure as one transaction suite. |
| `tests/test_persistence_migration_transactions.py` | 817 | Retain. Its migration-only crash and divergence fixtures protect base/target convergence, lineage publication, source-path proof, one-rebase closure, and evidence retention through the same version-two transaction boundary. |

`packaging/check_architecture.py` is also approximately 800 lines. Its AST
data model and ownership/path analysis are already split into
`architecture_ast.py` and `architecture_ownership.py`; the remaining module is
the cohesive policy orchestrator and rule implementation. Another split would
create a small rule-registration framework without a concrete second use.

## Specification-parity matrix

`Pass` means the cited current behavior, tests, documentation, or artifact
evidence satisfies the complete authority range. Ranges are explicit so no
numbered goal, invariant, package entry, criterion, or approved decision is
implicitly omitted.

| Authority | Owning change set | Code evidence | Test evidence | Docs | Status |
| --- | --- | --- | --- | --- | --- |
| Design 2.1, goals 1-15 | CS-03, CS-07 through CS-22 | Final `core/`, `cli/`, provider, service, HTTP, path, persistence, and renderer owners | Architecture, packaging, provider, CLI, service, time, and persistence suites | README development and operations sections | Pass |
| Design 2.2, every non-goal | CS-04, CS-06, CS-22 | No generic repository, container, locator, mapper, event bus, settings object, timestamp utility, retry framework, plugin system, or speculative symmetry exists | Architecture deliberate-violation test and source-shape checks | Design 2.2 and plan 9 remain explicit | Pass |
| Design 3.1, source-first decisions | CS-00, CS-07 through CS-09, CS-14, CS-22 | Locked versions and behavior match the recorded source evidence | Dependency, packaging, native-platform, and compatibility tests | Tracked research records and this completion evidence | Pass |
| Design 3.2, reuse and abstraction | CS-04, CS-12A, CS-13, CS-18, CS-21 | Shared concepts have proven owners; feature-local outcomes remain local; no generic utility layer exists | Service and architecture tests protect the selected owners | Design 14 and package-layout docs | Pass |
| Design 3.3, type hygiene | CS-06, CS-12A through CS-22 | PEP 695 aliases/generic owner, explicit unions, stdlib enums, `Path`, aware datetime, `HTTPMethod` | Ruff ANN, `ty`, architecture HYG and model-contract cases | AGENTS coding rules | Pass |
| Design 3.4, error handling | CS-01, CS-10, CS-13A, CS-17A through CS-19 | Typed missing, malformed, unreadable, conflict, partial, unsupported, transport, and recovery states; no swallowed broad failure | Error, provider, lifetime, HTTP, doctor, and persistence recovery suites | Networking and persistence recovery guides | Pass |
| Design 3.5, test quality | Every change set | Tests target public command, service, boundary, transaction, and artifact outcomes | Private-helper, inert app-existence, redundant order, and copied render tests were removed or consolidated | Plan 6 and AGENTS testing rules | Pass |
| Design 3.6 and 3.7, module and documentation hygiene | CS-04, CS-06, CS-22, CS-23 | No production module exceeds 1000 lines; no blanket suppression, dead block, legacy annotations import, or stale converted file remains | Ruff, Bandit, architecture, codespell, and Markdown gates | Cohesion review above and updated contributor guide | Pass |
| Design 3.8, complete-product delivery | CS-10 through CS-23 | Migration includes discovery, assessment, explicit execution, recovery, rollback, doctor output, aliases, artifacts, and platform gates | End-to-end transaction, CLI, compatibility, wheel, and platform suites | README and operational guides | Pass |
| Design 3.9, research and dependency decisions | CS-07 through CS-09, CS-22, CS-23 | Pydantic, urllib3, platformdirs, Portalocker, and pywin32 are narrow and locked; custom architecture checks were selected after comparison | Dependency ownership and Homebrew closure tests | Tracked research with primary sources and reversal conditions | Pass |
| Design 3.10, configuration, policy, and runtime paths | CS-09, CS-10A, CS-12, CS-19, CS-22 | No global settings object; paths, retry, terminal width, provider endpoints, maintenance, and scheduler policy stay with their owners | Architecture CFG/PATH/TIME checks and path tests | README configuration and persistence guide | Pass |
| Design 3.11 and 3.12, baseline and required fixes | CS-01 through CS-06, CS-13, CS-13A, CS-17A | Exhaustive daemon operation, explicit empty registries, shared width, safe identities, normalized provider time, explicit lifetime failure | Daemon, heartbeat, help/branding, docs, core, provider, and lifetime tests | Design execution records | Pass |
| Design 4.1, every target package entry | CS-11 through CS-22 | Every listed package/module exists under its final owner; all listed obsolete flat paths are absent | Architecture PKG/CLI checks and exact source/sdist/wheel member checks | README repository layout and AGENTS | Pass |
| Design 4.2, discovery and precedence | CS-09, CS-12, CS-19 | Frozen `ApplicationPaths`; generation-aware native, compatibility, and prototype candidates; side-effect-free discovery | Path and location generation matrices, WSL probe | Persistence guide path and upgrade sections | Pass |
| Design 4.2, migration and recovery | CS-14, CS-18, CS-19 | Sole migration service, injected private-auth port, dual locks, authority-last journal, lineage, one bounded rebase | Location service, migration transaction, credential transaction, doctor, and v0.6 suites | Persistence migration, recovery, and rollback sections | Pass |
| Design 5, core contracts | CS-12A, CS-13 | Narrow infrastructure-free models/types/expiry/time; feature-local models remain outside core | Core model/type/expiry and architecture dependency tests | README layout and design 5 | Pass |
| Design 6, schemas and serialization | CS-07, CS-13, CS-14, CS-17A, CS-19 | Boundary-local Pydantic adapters, recursive JSON vocabulary, strict version-one envelope, canonical persistence timestamps | Schema, JSON, provider, transaction-journal, and compatibility tests | README schema example and persistence guide | Pass |
| Design 7, provider packages | CS-16, CS-17, CS-17A, CS-19 | Provider-owned Claude/Codex credentials, usage, schemas, heartbeat; Codex auth migration adapter; explicit registries | Claude, Codex, auth-migration, heartbeat, and registry tests | README provider behavior | Pass |
| Design 8, CLI package and hierarchy | CS-18A, CS-20 | Registration-only app, strict typed lazy contexts, close-once `Composed[T]`, focused command owners, canonical provider groups and thin aliases | Help no-composition, close-once, blocked doctor, command registration, alias equivalence, and wheel tests | README commands and exact deprecation lifecycle | Pass |
| Design 9.1, usage service | CS-15, CS-21 | Typed service owns collection and partial results; command owns exit/output; renderer owns presentation | Usage service, command error, and render tests | README report behavior | Pass |
| Design 9.2 through 9.5, credentials and persistence | CS-14, CS-18, CS-19 | Canonical credential workflow, Codex bridge, durable `persist`, strict assessment, transactional private bundles, explicit migrations | Credential service, account store, coordinator, filesystem, native, and transaction suites | Token maintenance and persistence guides | Pass |
| Design 10, HTTP infrastructure | CS-08, CS-11 | Invocation-scoped pools, TLS/URL validation, proxy selection, bounded bodies, closed operations, monotonic deadline, one retry executor | Client and retry matrices cover attempts, waits, HTTP-date and delta guidance, payload shape, pool closure, and safe failures | Networking guide | Pass |
| Design 11, presentation | CS-03, CS-21 | Renderers return values, receive explicit time/read models, and perform no provider, persistence, network, filesystem, or output I/O | Branding, usage render, heartbeat human/JSON/quiet, doctor human/JSON, and architecture import tests | TUI authority and README | Pass |
| Design 12, dependency direction | CS-11 through CS-22 | Core, provider, persistence, HTTP, service, CLI, path, clock, and renderer directions match the diagram | Each architecture rule rejects a deliberate broken tree and passes the real tree | Architecture research and AGENTS layout | Pass |
| Design 13, errors and statuses | CS-01, CS-13, CS-15A, CS-17A through CS-19 | `UsageError` root retained; closed provider, daemon, refresh, expiry, heartbeat, doctor, location, and exit vocabularies | Status vocabulary, parsing, daemon, usage, doctor, and migration tests | README troubleshooting and recovery codes | Pass |
| Design 14, confirmed reuse decisions | CS-03, CS-10A, CS-12A, CS-13, CS-14, CS-18, CS-21 | Each tabled concept has one named owner; semantically different timestamp encoders remain separate | Architecture singleton/owner checks and owner-specific behavior tests | Design 14 ownership table remains current | Pass |
| Design 15.9, safety and hygiene criteria | CS-01 through CS-06, CS-22 | Correctness fixes, reserved fixtures, dead-surface removal, clean gates | Daemon, registry, width, docs, architecture, Ruff, and type gates | AGENTS and plan execution records | Pass |
| Design 15.9, core/schema/persistence/HTTP/path criteria | CS-07 through CS-14, CS-19, CS-22 | Strict boundaries, no false zero/absence, canonical time, explicit path injection, qualified filesystem, migration and HTTP closure | Schema, lifetime, HTTP, path, location, native, recovery, compatibility, and architecture suites | Networking and persistence guides | Pass |
| Design 15.9, usage/provider/credential criteria | CS-15 through CS-18, CS-21 | Typed partial results, provider-owned parsing, explicit refresh outcomes, canonical credentials, no UI dependency | Usage, provider, credential, refresh, heartbeat, and render suites | README provider and report sections | Pass |
| Design 15.9, CLI criteria | CS-18A, CS-20, CS-22 | Exact command ownership, lazy service contexts, no-composition help/version, provider discovery, aliases, both entry paths | CLI lifecycle/help/registration/alias tests and isolated exact wheel | README commands and AGENTS | Pass |
| Design 15.9, presentation/final criteria | CS-21 through CS-23 | Rich builders return renderables, JSON is typed, command owns I/O, source is suppression-free, no unused framework remains | Render, doctor, heartbeat, architecture, complete local and CI gates | README and this record | Pass |
| Design 16.1, load-bearing behavior | Every implementation change set | Public boundaries expose every accepted behavior and failure state | Focused behavior suites named throughout this matrix | Plan 6 test-selection record | Pass |
| Design 16.2 through 16.4, moves, consolidation, and fixtures | CS-05, CS-15 through CS-22 | Tests follow final module owners; repeated fixtures remain local unless they have repeated consumers; sensitive identities are synthetic | Docs identity gate, type/lint gate, and full pytest collection | AGENTS testing rules | Pass |
| Design 16.5, every architecture check | CS-22 | `packaging/check_architecture.py` plus focused AST and ownership modules enforce the complete rule set | One combined deliberate broken snapshot proves the exact rule-id set; warning, real-tree, and command-surface tests add distinct value | Architecture enforcement research | Pass |
| Design 17, every quality gate | CS-06, CS-11, CS-14, CS-22, CS-23 | Local aggregate, exact artifacts, source member checks, dependency locks, platform workflow | Full pytest, pre-commit, Markdown, audit, compatibility, wheel, Homebrew, and CI evidence | Verification sections above | Pass |
| Design 18, every security and operational constraint | CS-05, CS-08, CS-10 through CS-19, CS-22 | HTTPS only, redacted errors, bounded payloads, owner-only files, qualified filesystems, no active-login overwrite, scheduler quiescence, retained evidence | Bandit, secret scan, HTTP, provider, credential, filesystem, migration, reset, and synthetic CLI tests | README security, networking, persistence guides | Pass |
| Design 21, approved decisions 1-15 | CS-07, CS-08, CS-11 through CS-20 | Core/provider/CLI/schema/HTTP hierarchy and dependency selections are final | Architecture, package, provider, command, schema, and HTTP tests | Decision closure above and design 21 | Pass |
| Design 21, approved decisions 16-30 | CS-09 through CS-20 | Expiry/time/path ownership, lazy composition, generation precedence, explicit schema migration, rollback snapshots, Portalocker/pywin32 adoption | Time, path, context, persistence, native, compatibility, and CLI tests | Research, networking, and persistence guides | Pass |
| Design 21, approved decisions 31-45 | CS-13 through CS-22 | Qualified durability, labels/models/statuses, credential workflows, strict contexts, close-once resources, explicit registries, narrow seams, atomic CLI conversion, concise tests | Native filesystem, core, provider, credential, context, help, packaging, and architecture suites | AGENTS, README, design execution records | Pass |
| Usage-TUI specification, all approved visuals | CS-03, CS-21 | Canonical robot remains in `branding.py`; usage renderer moved under `usage/` without redesign; provider counts remain in panel titles; no global summary was introduced | Exact robot rows/copy, panel singular/plural counts, width floor, narrow fallback, heat bands, reset display, and one-source tests | Usage-TUI design remains authoritative | Pass |
| Every conditional decision | CS-07 through CS-09, CS-19, CS-22 | Three approved GO dependencies are present only behind their owners; settings, Tenacity, stacked retries, and hidden relocation remain absent | Dependency ownership, path/migration, HTTP policy, architecture, packaging, and platform tests | Conditional closure table above | Pass |

## Completion conclusion

All architecture change sets have production implementations and behavior
proof. Native migration is an implemented GO, not a dormant branch. The source,
sdist, and wheel contain the final packages and no stale converted modules.
Users can discover canonical commands, diagnose location state, migrate or
recover explicitly, and prepare a verified v0.6.0 rollback without consulting
implementation source.

The final release remains automation-owned: merging the completed work through
the normal repository process lets Release Please update `CHANGELOG.md`, the
Python version sources, and the manifest for 0.7.0. The compatibility aliases
remain through all 0.8.x releases and are removed in 0.9.0.
