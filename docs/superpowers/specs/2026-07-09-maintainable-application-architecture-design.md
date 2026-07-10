# Design Spec — Maintainable Application Architecture

- **Status:** **Approved**
- **Date:** 2026-07-09
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Scope:** Repository-wide Python application architecture
- **Evidence base:** `develop` at
  `42cd01eb17c7903b385b1b4e259cf5b0c64126c5`
- **Evidence commit subject:** `feat(cli): add shared robot branding`
- **Evidence mode:** Read-only architecture and maintainability analysis
- **Evidence-tree state:** Clean and tracking `origin/develop`
- **Evidence status:** All decision-relevant evidence and conclusions are
  inlined here; this repository document is the durable design authority
- **Publication state:** Tracked and approved at execution base
  `73ce06891747a0571276b35c3f54c7de2c4e188f`
- **Related design:** [Usage TUI Redesign][usage-tui-design]; this spec does
  not alter its visual contract
- **Next step:** Execute the matching implementation plan in dependency order

---

This specification preserves the complete evidence, target, constraints, test
strategy, and migration decisions so implementation can continue across
session boundaries without reconstructing the architecture discussion.

## 1. Context and problem

The package has sound local components, but its ownership boundaries have not
kept pace with its feature set.

Current strengths include:

- separate Claude and Codex provider adapters;
- a heartbeat feature with distinct policy, service, adapter, and rendering
  concerns;
- injected HTTP, filesystem, scheduler, and provider boundaries;
- a shared branding component that does not load credentials or application
  state;
- distinct daemon and maintenance modules with cohesive operational concerns;
- predictable source and test layouts;
- meaningful behavior around refreshes, account identity, maintenance,
  heartbeat scheduling, command output, and machine-readable output.

The migration extends the repository's strongest local pattern, especially
heartbeat's model, service, adapter, and render separation. It does not impose
an unrelated framework on otherwise sound components.

The dominant problem is `src/sidekick_usages/cli.py`. At the evidence commit it
is 2274 lines and combines:

- Typer application construction;
- global and per-invocation context;
- command declarations;
- account selection;
- provider selection;
- usage collection;
- retry and refresh policy;
- credential import and export;
- Codex auth-file management;
- persistence coordination;
- rendering decisions;
- status aggregation;
- exit-code mapping.

The module is both the CLI adapter and much of the application layer. This
encourages tests to patch private CLI helpers and makes otherwise independent
features change together.

Provider ownership is also fragmented:

- `providers/claude.py` contains credential discovery, parsing, usage,
  refresh, and setup-token behavior;
- `heartbeat/claude.py` contains additional Claude protocol behavior;
- `providers/codex.py` contains credential detection, usage, refresh,
  `CODEX_HOME`, `auth.json`, JWT, and response-parsing behavior;
- `heartbeat/codex.py` contains additional Codex protocol behavior;
- Codex-specific auth workflows also live in the CLI.

Shared product models are owned by adapters:

- `Account` lives in `store.py`;
- `DetectedCredentials` lives in `providers/base.py`;
- `UsageWindow` and `UsageReport` live in `report.py`;
- `FetchFailure` lives in the renderer even though application logic creates
  it.

Several correctness problems are architectural rather than cosmetic:

- an unknown daemon-operation string falls through to uninstall behavior;
- account-store migration can fail without surfacing the failure;
- absent and malformed credential files can produce the same state;
- expiry and lifetime failures can appear as valid zero values;
- an explicit empty heartbeat registry can be replaced by production defaults;
- provider-native expiry values use inconsistent units;
- repeated `upsert()` plus `save()` calls permit forgotten persistence;
- raw JSON crosses boundaries through `Any` and unchecked casts.

The design must correct these issues without replacing the application with a
generic framework.

## 2. Goals and non-goals

### 2.1 Goals

The architecture must:

1. Make `sidekick_usages.cli` a dedicated package with explicit application
   composition, context, help adaptation, and command modules.
2. Group Claude and Codex integration behavior under provider-owned packages.
3. Establish a narrow `core/` package for shared product models, type
   vocabulary, and pure cross-feature product policy.
4. Keep provider and persistence schemas at their external boundaries.
5. Move orchestration into typed application services.
6. Represent failures and partial success explicitly.
7. Preserve human, JSON, quiet, scheduler, and installed-entry-point behavior
   unless this spec explicitly changes it.
8. Make provider-specific commands discoverable and consistently organized.
9. Normalize time and status concepts before they enter shared application
   logic.
10. Keep the fewest load-bearing tests at stable public boundaries.
11. Enforce module size, type hygiene, and suppression rules mechanically.
12. Evaluate mature dependencies before building generic validation,
    serialization, configuration-source, or cross-platform path-discovery
    machinery.
13. Preserve one reusable HTTP façade while adopting mature transport or retry
    machinery when it reduces owned code and passes the dependency gate.
14. Give application-owned filesystem locations one immutable, injected owner
    while preserving explicit provider and scheduler path ownership.
15. Separate time acquisition from provider, persistence, and presentation
    timestamp serialization.

The complete target is designed before implementation begins. Delivery phases
exist to keep the migration safe and reviewable; they are not permission to
stop at an underpowered intermediate architecture.

### 2.2 Non-goals

This design does not introduce:

- a generic repository abstraction;
- a unit-of-work framework;
- a generic mapper framework;
- a dependency-injection container;
- a service locator;
- a base command class;
- dynamic command discovery;
- an entry-point plugin system;
- an event bus;
- a result-monad framework;
- a generic terminal-screen framework;
- a generic resilience framework or stacked retry engines;
- a catch-all `utils.py`;
- a project-wide `schemas.py`;
- a miscellaneous `core/` bucket;
- a global settings object or configuration service locator;
- a hand-written cross-platform application-directory framework;
- a generic timestamp-formatting utility;
- filesystem relocation hidden inside the initial path-centralization change;
- a project-wide model file containing feature-local outcomes;
- forced identical provider internals;
- speculative provider or command hooks;
- shared test fakes without repeated concrete need.

It does not mechanically split `daemon.py` or another cohesive module without
a concrete ownership boundary, and it does not authorize behavior changes
unrelated to the identified defects or accepted architecture boundaries.

It also does not redesign the approved usage panels, robot masthead, heat
encoding, or provider-local account counts. Those remain governed by the
usage-TUI design spec.

## 3. Design principles and enforceable invariants

### 3.1 Source-first decisions

Use this evidence order for consequential decisions:

1. live source, tests, configuration, lockfile, and runtime behavior in the
   named checkout;
2. current repository design and operational documentation;
3. official standards, vendor documentation, release notes, and upstream
   source;
4. canonical maintainer repositories, changelogs, issue trackers, and security
   policies; and
5. high-quality secondary analysis only when primary evidence is insufficient.

Before changing a concept:

1. inspect the live source, tests, configuration, and lockfile;
2. search the relevant package for the exact concept name;
3. read two or three neighboring files;
4. verify current behavior;
5. consult current primary documentation for unstable external facts;
6. record the evidence behind consequential dependency decisions.

Remembered APIs and architectural summaries are not ground truth.

Record the branch, commit, dependency version, retrieval date, and source URL
needed to reproduce a consequential decision. Research the web when a library,
standard, security posture, compatibility claim, or ecosystem status may have
changed. Compare publication and release dates, and label architectural
inference separately from sourced fact.

Before applying external guidance:

- confirm the installed or locked version;
- inspect how the dependency is used locally;
- verify supported Python and operating-system versions;
- reconcile the guidance with tests and packaging; and
- verify behavior after implementation.

### 3.2 Reuse and abstraction

- Search before adding a constant, helper, map, type, or service.
- Treat a second implementation of the same concept as a defect.
- Search the exact term before searching synonyms.
- Reuse concepts that are semantically identical, not merely similar-looking.
- Match neighboring naming, structure, comment density, and error vocabulary.
- Apply the rule of three before extracting reusable machinery.
- Extract a domain rule when it already has repeated concrete consumers.
- Prefer small, domain-focused classes and functions.
- Do not extract a repeated one-line expression unless it is one domain rule.
- Do not add speculative parameters, hooks, flags, or extension points.
- Keep an abstraction private until more than one module needs it.

The rule of three limits abstraction, not product capability. A rich product
feature may require new supporting boundaries when those boundaries serve
concrete behavior.

### 3.3 Type hygiene

- No `Any` in application or domain code.
- No unjustified `cast(...)` or equivalent type escape.
- Exported functions and public methods have explicit parameter and return
  types.
- Optional state is explicit.
- Prefer illegal states to be unrepresentable.
- Use Python 3.14 native PEP 695 aliases and generics.
- Prefer `Path`, aware `datetime`, `StrEnum`, and `IntEnum` to magic strings or
  numbers. HTTP request construction uses `HTTPMethod.POST` or the appropriate
  standard-library member, never a magic method string.
- Use concrete runtime validation at untrusted boundaries; annotations alone
  are not validation.

### 3.4 Error handling

- Do not swallow failures.
- Do not convert a real error into a plausible default value.
- Distinguish missing, malformed, unreadable, expired, rejected, unsupported,
  and transient states.
- Fail closed when credentials or persisted state cannot be trusted.
- Preserve the existing `UsageError` vocabulary and extend it only for
  concrete missing cases.
- CLI adapters render errors; services and adapters return or raise typed
  states.

### 3.5 Tests

- Every test must fail for a meaningful behavioral reason.
- Test depth follows acceptance criteria.
- Prefer service and command boundaries to private helpers.
- Keep exact-output assertions only for intentional product contracts.
- Delete redundant, inert, implementation-coupled, and brittle tests.
- Test code follows the same type and maintenance standards as production code.
- Never add tests merely to increase a count or percentage.

### 3.6 Module hygiene

- 1000 lines is a hard module limit.
- Approximately 800 lines triggers a split review.
- No dead code, stale comments, unused imports, or commented-out blocks.
- No blanket lint, type, or security suppressions.
- A necessary suppression identifies one rule and explains the constraint.

### 3.7 Docstrings and comments

- Keep code, comment, and docstring lines within 79 characters.
- Describe what a unit does, not how it restates its signature.
- Use concise Sphinx fields only when they add information.
- Use `:param <name>:`, `:returns:`, and `:raises <Exception>:` consistently.
- Omit `:returns:` for a `-> None` command and omit empty field stubs.
- Fix or delete comments when their claims no longer match behavior.

### 3.8 Complete-product delivery

Design the cohesive, feature-rich product outcome before dividing work into
delivery phases. For every substantial feature:

1. define the complete user experience and its concrete "wow factor";
2. identify its model, integration boundaries, failure states, observability,
   cross-platform behavior, and migration path;
3. change an inadequate current boundary when that is necessary to support a
   valuable capability;
4. define final-state acceptance criteria before selecting phase boundaries;
5. make every phase production-quality and compatible with the final
   architecture; and
6. complete supporting refactors instead of preserving accidental constraints.

Do not choose an implementation only because it produces the shortest diff.
Evaluate long-term correctness, product experience, operability,
maintainability, and feature ceiling. Equally, do not mistake unnecessary
complexity for senior design: every mechanism must have a concrete role in the
complete experience.

The rule of three limits reusable abstractions, not product ambition.

### 3.9 Durable research and dependency decisions

Decision-relevant research is inlined in the design specification that depends
on it. When the evidence is too extensive for one specification, persist a
git-tracked supplementary companion record under:

```text
docs/superpowers/research/<topic-slug>/
├── research.md
├── sources.md
└── decision.md
```

`research.md` records the question, local code evidence, findings, synthesis,
and recommendation. `sources.md` records owners, URLs, retrieval dates,
versions, and the claims each source supports. `decision.md` records options,
tradeoffs, the build-or-adopt choice, consequences, and reversal conditions.

Add focused evidence files only when useful. Do not persist an uncurated search
dump. Every record includes:

- the exact decision question;
- repository branch and commit;
- relevant source paths and line references;
- search date and scope;
- primary-source links;
- version and platform constraints;
- rejected findings and the reason for rejection;
- clearly labelled architectural inferences;
- the decision and conditions that require it to be revisited.

Research is complete only after it is reconciled with the live codebase. If
the code or dependency state changes materially before implementation, refresh
the affected research.

Temporary or ignored artifacts are never normative references. Before a
specification or plan depends on information discovered there, inline every
needed fact, measurement, tradeoff, decision, and reversal condition in the
specification. A tracked companion may preserve supplementary evidence, but it
never substitutes for inlining information the design or implementation needs.

Before implementing substantial generic functionality, compare:

1. the standard library;
2. existing locked dependencies;
3. plausible, actively maintained upstream libraries; and
4. a focused in-house implementation.

For GitHub-hosted candidates, inspect the canonical repository. Stars are a
discovery signal, not proof of fitness. Record:

- functional fit and feature ceiling;
- API quality and stability;
- maintenance and release activity;
- maintainer depth and governance;
- issue and pull-request responsiveness;
- Python 3.14, Linux, macOS, and Windows compatibility;
- dependency size and transitive supply-chain exposure;
- security policy, advisories, provenance, and update process;
- license compatibility;
- performance and resource behavior;
- testability and observability;
- wheel, Homebrew, CLI, and TUI integration impact;
- migration cost and lock-in;
- expected maintenance cost over two to three years;
- an escape path if the project stagnates.

Prefer adoption when a mature, compatible dependency materially reduces owned
code without compromising behavior, security, packaging, or operability.
Prefer a focused implementation when the capability is a differentiator,
candidate fit or risk is unacceptable, or integration costs more than the
cohesive owned surface.

Keep an adopted integration narrow enough to replace, without building a
wrapper that reimplements the dependency. If building, record why the
evaluated candidates were rejected. Never select a dependency from reputation
or memory alone.

### 3.10 Configuration, policy, and runtime paths

Configuration is not a synonym for every constant or tunable value. Ownership
follows the behavior a value governs:

- pure provider-neutral product policy may live in `core/`;
- refresh workflow policy remains with maintenance;
- retry, deadline, and timeout policy remains in `http/`;
- terminal-width policy remains in `cli/help.py`;
- provider endpoints, native homes, and wire settings remain provider-owned;
- scheduler identifiers and installation locations remain daemon-owned; and
- Sidekick-owned state, credential-copy, and cache locations are discovered by
  top-level `paths.py`.

Reading environment variables, configuration files, CLI overrides, secrets,
or operating-system directories is an outer-boundary responsibility. Core
policy may accept an already validated value, but core never discovers or
loads that value.

The current application has no cohesive, user-facing, multi-source settings
contract. Do not create `settings.py`, a `configuration/` package, or a global
`AppSettings` object merely to collect unrelated feature constants. If such a
contract emerges, it must have concrete fields, sources, precedence rules, and
consumers. Its immutable validated model is loaded once at the application
composition boundary. Its loader belongs at a dedicated configuration
boundary, and `cli/app.py` composes narrow values into the dependencies that
need them.

Persisted account records are durable application state, not runtime settings,
even though the current physical file lives below a directory named
`.config`. Credential-bearing private auth copies are also not ordinary cache
entries. The path model records their distinct lifecycle and security
requirements instead of inferring semantics from directory names.

### 3.11 Current evidence snapshot

All counts describe the evidence commit and must be refreshed if the branch
advances before implementation.

| Module | Lines | Assessment |
|---|---:|---|
| `src/sidekick_usages/cli.py` | 2274 | Hard size-limit violation |
| `src/sidekick_usages/render.py` | 739 | Near the review threshold |
| `src/sidekick_usages/daemon.py` | 668 | Cohesive enough for now |
| `src/sidekick_usages/providers/codex.py` | 587 | Package extraction justified |
| `src/sidekick_usages/providers/claude.py` | 501 | Package extraction justified |
| `src/sidekick_usages/heartbeat/service.py` | 472 | Local pattern to reuse |

`cli.py` is the only production module over the 1000-line hard limit. The
repository's own token-maintenance documentation says the CLI should remain
thin (`docs/token-maintenance.md`, near line 465 at the evidence commit).

The measured type and lint baseline is:

- 11 production `cast(...)` calls;
- `Any` at JSON and third-party boundaries with inward leakage;
- only 2 of 28 package modules using
  `from __future__ import annotations`;
- 15 focused Ruff `E501` findings in source and tests;
- 8 focused Ruff `W505` findings at the 79-character document-line limit,
  comprising 6 source findings and 2 test findings;
- 98 focused Ruff annotation findings, mostly in tests, including a production
  `**kwargs: Any` signature in `cli_help.py`;
- 10 `# noqa` suppressions;
- a global `B905` ignore with no corresponding `zip(...)` usage.

No blanket `# type: ignore`, `# nosec`, or `except Exception` pattern was found
in the inspected tree. That is a property to preserve. The baseline calls for
staged boundary cleanup, not a broad annotation campaign followed by new
suppressions.

An implementation refresh on 2026-07-09 resolved the future-annotations
inconsistency. Python 3.14 evaluates annotations lazily by default, while the
future import selects the older stringized model and is scheduled for eventual
deprecation and removal.[python-314-annotations] The repository's configured
`pyupgrade --py314-plus` gate also removed all five imports present at the
execution base. The repository therefore uses native Python 3.14 deferred
annotations and rejects new `from __future__ import annotations` imports. This
decision updates the repository rule and does not change the audited baseline
above.

### 3.12 Required correctness fixes

These findings are acceptance criteria for the architecture, not optional
cleanup.

#### Daemon operations must be exhaustive

At approximately `cli.py:1401`, an unknown operation string reaches the
uninstall fallback. Replace strings with a closed `DaemonOperation` vocabulary
and exhaustive dispatch. Reject unknown input before the operation layer;
there is no destructive default.

#### Store migration must fail loudly

At approximately `store.py:263-271`, migration failure is swallowed. Migration
either completes and persists valid state or raises a typed persistence error.
Unreadable, malformed, or partially migrated state never becomes a default
object or an apparently valid empty store.

#### Credential absence and corruption must remain distinct

Claude and Codex flows must distinguish:

- no credential file;
- an unreadable file;
- malformed JSON;
- missing required fields;
- expired credentials;
- remotely rejected credentials.

These states require distinct user guidance and maintenance outcomes.

#### Lifetime and expiry failures must not become zero

Zero is a valid value and cannot represent a parse or I/O failure. Preserve
`VALID`, `EXPIRED`, `UNKNOWN`, and `INVALID` as explicit states, paired with an
aware time or typed failure in a representation that prevents contradictory
combinations.

#### Empty heartbeat registries must remain empty

Replace truth-value fallback with an explicit `is None` decision. `None` means
no registry was supplied; an empty mapping is a deliberate injected registry.

#### Help and masthead widths must share one policy

At a 120-column terminal, the masthead can use 120 columns while Typer remains
at 80. The existing `_help_width` concept becomes the canonical policy for the
masthead, help panel, option layout, and divider. Do not add a competing width
helper.

#### Test and documentation identities must be reserved

Replace realistic-looking account and provider identifiers with the exact
reserved 30-character fixture:

```text
long.account.name@example.test
```

The evidence search found affected material in `tests/test_render.py`,
`tests/test_check_errors.py`, and the existing TUI specification and plan.
Refresh the search before editing.

#### Provider-native time units must stop at provider boundaries

The current `Account.expires_at` integer represents milliseconds for Claude
and seconds for Codex. Convert provider-native values at schema or adapter
boundaries. Shared application policy consumes an aware `datetime` or one
canonical normalized unit.

## 4. Chosen architecture

The package is organized around four ownership axes:

1. shared product vocabulary and pure cross-feature policy in `core/`;
2. user-interface adaptation in `cli/`;
3. provider integrations in `providers/`;
4. feature services and infrastructure at explicit boundaries.

### 4.1 Target package structure

```text
src/sidekick_usages/
├── __init__.py
├── __main__.py
├── branding.py
├── errors.py
├── core/
│   ├── __init__.py
│   ├── models.py
│   ├── types.py
│   └── expiry.py
├── serialization/
│   ├── __init__.py
│   └── json.py
├── cli/
│   ├── __init__.py
│   ├── app.py
│   ├── context.py
│   ├── help.py
│   ├── token_input.py
│   └── commands/
│       ├── __init__.py
│       ├── usage.py
│       ├── accounts.py
│       ├── credentials.py
│       ├── heartbeat.py
│       ├── maintenance.py
│       ├── doctor.py
│       ├── daemon.py
│       ├── updates.py
│       ├── claude.py
│       └── codex.py
├── providers/
│   ├── __init__.py
│   ├── base.py
│   ├── registry.py
│   ├── claude/
│   │   ├── __init__.py
│   │   ├── provider.py
│   │   ├── credentials.py
│   │   ├── usage.py
│   │   ├── heartbeat.py
│   │   └── schemas.py
│   └── codex/
│       ├── __init__.py
│       ├── provider.py
│       ├── auth.py
│       ├── usage.py
│       ├── heartbeat.py
│       └── schemas.py
├── persistence/
│   ├── __init__.py
│   ├── account_store.py
│   ├── schemas.py
│   └── migrations.py
├── credentials/
│   ├── __init__.py
│   └── service.py
├── usage/
│   ├── __init__.py
│   ├── models.py
│   ├── service.py
│   └── render.py
├── heartbeat/
│   ├── __init__.py
│   ├── models.py
│   ├── ports.py
│   ├── service.py
│   └── render.py
├── maintenance.py
├── doctor.py
├── daemon.py
├── lifetime.py
├── clock.py
├── paths.py
├── http/
│   ├── __init__.py
│   ├── client.py
│   └── retry.py
└── update.py
```

The `cli/`, `core/`, provider, persistence, credentials, usage, and heartbeat
boundaries are deliberate. Smaller cohesive modules become packages only when
their real responsibilities justify the move.

Top-level `paths.py` owns Sidekick-managed durable-state, private-auth, and
cache locations that have multiple consumers. Provider-native homes remain in
their provider packages, and scheduler installation paths remain with daemon
adapters. This is a focused path owner, not a generic filesystem utility
module.

Top-level `clock.py` owns the narrow time-acquisition port and its production
system implementation. It returns aware UTC `datetime` values and owns no
timestamp parsing, string formatting, persistence schema, provider wire shape,
sleeping, or scheduling behavior.

```python
class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current aware UTC time."""
        ...


class SystemClock:
    def now(self) -> datetime:
        """Return the current aware UTC time."""
        return datetime.now(UTC)
```

Application services obtain one `now` value for one policy decision and pass
that value into pure core functions. This prevents boundary comparisons from
observing different instants during one decision.

The wall clock is not the HTTP retry timer. `http/retry.py` owns an injected
monotonic time source for elapsed deadlines and a sleeper for waits. Wall-clock
adjustments must not extend or shorten HTTP retry budgets.

### 4.2 Application paths

`paths.py` exposes one frozen value containing concrete Sidekick-owned
locations. The approved semantic roles are:

At the evidence commit, `store.py` defines the current Sidekick directory and
account file, `cli.py` derives the private Codex directory from that
representation, and `lifetime.py` independently reconstructs the Sidekick
root for its cache. `store.py` also owns the older `cc-usage` prototype
migration source. These are repeated concrete consumers, not a speculative
path abstraction.

```python
@dataclass(frozen=True, slots=True)
class AccountLocations:
    canonical: Path
    existing_sidekick: Path
    prototype_cc_usage: Path


@dataclass(frozen=True, slots=True)
class PrivateCodexLocations:
    canonical: Path
    existing_sidekick: Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    accounts: AccountLocations
    private_codex: PrivateCodexLocations
    lifetime_cache_file: Path
```

The three account-location roles are explicit because the live application
already has a current Sidekick file and a prototype `cc-usage` migration
source; a native `platformdirs` location would add a third candidate. During
compatibility-preserving centralization, `canonical` and
`existing_sidekick` may resolve to the same path.

Account sources are generation-aware:

1. the native canonical store is authoritative after native migration;
2. the existing Sidekick store is the compatibility generation; and
3. the prototype `cc-usage` store is an import-only fallback considered only
   when neither authoritative generation exists.

Migration logic deduplicates equal resolved locations. If distinct canonical
and existing-Sidekick stores both exist, it proves equivalence or raises a
typed conflict. A malformed authoritative generation fails closed. A stale or
different prototype left behind after a successful historical import does not
conflict with an authoritative store, is never merged back, and is never
deleted automatically. If the prototype is the only candidate, its malformed
or unreadable state fails explicitly.

The two private-Codex roles are also explicit. During initial centralization,
`canonical` and `existing_sidekick` may be equal. Native migration gives them
different values so the coordinator can prove containment below the old root,
derive the corresponding destination below the new root, and avoid rebuilding
either root outside `paths.py`.

Consumers receive the concrete location they need. They do not import path
globals, call `Path.home()` for Sidekick-owned storage, or append private
filenames to a shared root. Tests construct `ApplicationPaths` directly with
temporary paths instead of monkeypatching import-time globals.

Path discovery:

- resolves the environment once at application composition time;
- has no directory-creation or file-writing side effect;
- distinguishes durable state, credential-bearing private auth, and
  safely-regenerable cache;
- preserves the current documented locations until an approved migration
  selects new canonical locations;
- never includes provider-native Claude or Codex homes; and
- never includes scheduler installation paths.

`cli/app.py` discovers production paths and uses them to construct the account
store, lifetime service, and credential workflows. The complete
`ApplicationPaths` value does not enter `AppContext`; commands receive the
already configured store, service, or narrow provider facade.

#### Cross-platform directory dependency decision

Current primary-source research retrieved on 2026-07-09 compared:

| Option | Benefits | Costs and risks | Decision |
|---|---|---|---|
| Standard library and local per-OS logic | No dependency | Owns XDG, macOS, Windows, WSL, override, and future-platform behavior | Rejected as the target |
| Keep one hard-coded `Path.home() / ".config"` root | Preserves current behavior with little code | Ignores native macOS and Windows conventions and keeps state/cache lifecycles conflated | Compatibility baseline only |
| `platformdirs` 4.10.0 behind `paths.py` | Focused config, data, state, and cache discovery across supported operating systems; Python 3.14 support | Canonical paths differ from the current public contract and require explicit migration | Preliminary adoption choice |
| `pydantic-settings` 2.14.2 | Typed multi-source settings, validation, and precedence | Solves a broader problem that the current application does not have | Deferred until a real settings contract exists |

`platformdirs` remains private to `paths.py`; consumers use `Path` and
`ApplicationPaths`. No provider, service, CLI command, persistence module, or
core module imports it directly.

The intended native semantic mapping is:

- account state and Sidekick-owned private Codex auth bundles are durable,
  credential-bearing application data rooted below `user_data_path`;
- lifetime aggregation totals are safely regenerable data rooted below
  `user_cache_path`; and
- no current artifact uses `user_config_path`, because the application has no
  user settings contract.

The spike verifies exact operating-system outputs, application name, author
argument, roaming behavior, and override semantics before approving physical
locations. It does not use directory names to reclassify durable state as
cache or settings. Discovery calls `platformdirs` path APIs with
`ensure_exists=False`; persistence, credential, and cache writers create only
the directories they own.

The implementation spike must record exact Linux, macOS, Windows, and WSL
outputs and decide the compatibility transition before changing a durable
location. It must classify each current artifact, preserve credential
permissions, and distinguish data that may be regenerated from data that must
be migrated.

If both authoritative generations exist, the application never silently
chooses one. It either proves they are equivalent under the migration contract
or raises a typed, actionable conflict surfaced by normal commands and
`doctor`. Migration never deletes a compatibility or prototype store
automatically.

#### Location-migration ownership

`paths.py` performs discovery only. `persistence/migrations.py` owns the
Sidekick durable-state location-migration workflow and receives
`ApplicationPaths` explicitly. It exposes:

- a read-only assessment describing every candidate, selected generation,
  equivalence or conflict, private-auth work, and recovery action; and
- one idempotent migration operation that coordinates account-state copying,
  provider-owned private-auth validation and copying, persisted-path rewriting,
  permission enforcement, and final atomic account-state commit.

`persistence/migrations.py` defines and consumes a narrow typed
`PrivateAuthMigrator` port for assessment, validated copy, permission
enforcement, and destination-collision reporting. The Codex auth adapter
implements that port. The current composition root injects the implementation;
the persistence package never imports a provider package directly.

The Codex auth adapter remains the owner of `auth.json` validation and
credential-file semantics. The migration coordinator does not adopt
provider-native home discovery or auth schema logic.

Non-diagnostic commands fail closed when assessment cannot select trustworthy
authoritative state. `doctor` consumes the read-only assessment and remains
able to report source, destination, conflict, partial work, and recovery action
without first requiring a successfully loaded account store. No second path or
migration service is introduced.

For an executable command, runtime composition assesses locations before
loading the store. An unambiguous one-source migration to an absent canonical
destination invokes the idempotent migration operation before store load. A
no-op assessment proceeds directly. A conflict or partial destination blocks
non-diagnostic commands while still permitting the composition needed by
`doctor`. Help and version bypass assessment entirely.

## 5. Shared core

### 5.1 `core/models.py`

`core/models.py` owns provider-neutral runtime product objects used across
features and adapters.

Initial models:

- `Account`;
- `DetectedCredentials`;
- `UsageWindow`;
- `UsageReport`.

These objects move from their current adapter-owned locations without carrying
adapter behavior with them.

Core models must:

- use explicit field types;
- avoid raw provider dictionaries;
- use aware `datetime` values for runtime time state;
- avoid provider-native expiry units;
- prevent credential values from appearing in default representations;
- expose behavior only when it is truly provider-neutral;
- avoid Rich, Typer, HTTP, filesystem, and serialization dependencies.

Provider, persistence, and presentation boundaries own their external epoch,
string, and display representations. Core models never store a formatted time
merely because an external schema does.

`core/` owns provider-neutral product vocabulary and pure cross-feature policy.
It does not discover configuration, load external settings sources, resolve
operating-system paths, perform filesystem or network I/O, or depend on
infrastructure frameworks. Operational configuration remains with the feature
or adapter whose behavior it governs and is composed at the application entry
point.

`Account` may remain mutable while refresh and heartbeat workflows update it,
but persistence is not one of its methods.

### 5.2 `core/types.py`

`core/types.py` owns shared type vocabulary, not every annotated class.

Initial candidates:

- `ProviderId` as a closed `StrEnum`;
- `AccountLabel` as a PEP 695 alias, `NewType`, or value object after its
  invariants are confirmed;
- genuinely cross-feature status or exit enums.

A plain alias improves readability but does not create runtime validation.
`NewType` improves static separation but also does not validate values.
A value object is justified only when labels have real normalization or
validation behavior.

Feature-specific statuses stay with their feature models. Provider wire
shapes and persisted records never belong in `core/types.py`.

### 5.3 `core/expiry.py`

Provider adapters normalize expiry into an aware UTC `datetime` before
constructing core models. `core/expiry.py` then owns only provider-neutral
classification:

- valid;
- expired;
- unknown; and
- invalid.

It is consumed by usage checking, maintenance, doctor, and heartbeat
readiness. Unknown providers and invalid provider-native units fail at the
provider or schema boundary; they never select a default normalization path.
Core expiry policy receives the current aware time as an explicit value. It
does not import the system clock, read configuration, or format timestamps.

An initial shape for caller analysis is:

```python
class ExpiryState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Expiry:
    state: ExpiryState
    at: datetime | None
```

Do not implement that sketch without checking real callers. If it permits
nonsense combinations such as `VALID` without a time or `UNKNOWN` with an
authoritative time, replace it with a discriminated representation or focused
state classes that make those combinations impossible.

### 5.4 Feature-local models

Use-case results remain beside the service that creates and consumes them:

- `usage/models.py` owns `UsageCheckResult` and `FetchFailure`;
- `heartbeat/models.py` owns targets, window state, probe results, and
  outcomes;
- `RefreshOutcome` remains with maintenance until that feature earns a package;
- `AccountDiagnostic` remains with doctor until that feature earns a package;
- daemon operation and platform results remain with daemon until a cohesive
  package split is justified.

If maintenance or doctor later earns a package during this migration, move the
corresponding outcome to `maintenance/models.py` or `doctor/models.py`. The
approved target keeps the current cohesive flat modules, so empty packages and
model files are not created merely for naming symmetry.

Do not create empty `models.py`, `types.py`, or `schemas.py` files to make
packages look identical.

## 6. Schemas, serialization, and runtime validation

### 6.1 Naming contract

The project uses these terms consistently:

| Name | Responsibility |
|---|---|
| `models.py` | Runtime product or use-case objects |
| `types.py` | Shared aliases, identifiers, and closed vocabulary |
| `schemas.py` | External or persisted representations |
| `ports.py` | Interfaces implemented by adapters |

Use plural `schemas.py` when a boundary contains multiple payloads or stored
formats. Use singular `schema.py` only for one cohesive schema.

### 6.2 JSON vocabulary

`serialization/json.py` owns the recursive JSON vocabulary:

```python
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
```

It may also own narrowly focused recursive validation required before a generic
JSON value enters a typed schema.

The aliases do not validate runtime data and must not be used to justify an
unchecked cast.

### 6.3 Boundary-local schemas

Provider payloads stay with providers:

```text
providers/claude/schemas.py
providers/codex/schemas.py
```

Examples include:

- local credential-file payloads;
- OAuth refresh responses;
- usage responses;
- Codex auth-file and JWT claim payloads.

Persisted account shapes stay with persistence:

```text
persistence/schemas.py
```

The persistence schema includes an explicit schema-version field. Migrations
accept each explicitly supported known stored shape and produce the current
stored shape. Unknown future versions fail closed.

Persistence schemas own conversion between aware runtime `datetime` values and
the stored account timestamp representation. Provider schemas own
provider-native epoch units and timestamp strings. Renderers own human display
formatting. `serialization/json.py` supplies JSON vocabulary; it does not
become the universal owner of provider, persistence, or presentation time
formats.

### 6.4 Build-versus-adopt decision

The project currently has no runtime validation dependency. Do not begin by
writing a custom schema framework.

The current candidates are:

| Option | Strength | Cost or risk | Current assessment |
|---|---|---|---|
| Standard library plus focused parsing | No new dependency; complete control | Reimplements nested validation, error paths, and serialization | Baseline, not the default winner |
| Pydantic `TypeAdapter` | Rich strict validation without requiring core models to inherit `BaseModel` | Runtime and packaging footprint; strict configuration required | Preliminary front-runner for diagnostic quality |
| cattrs | Keeps dataclasses independent from edge structuring rules | Converter configuration; fewer built-in constraints | Strong low-coupling alternative |
| msgspec | Fast typed decoding with no required transitive dependencies | Own conventions and compiled distribution | Candidate when measured performance or footprint wins |

No candidate is approved by popularity alone.

Before implementation, compare the candidates against:

1. current and legacy account files;
2. malformed and partially migrated account files;
3. Claude credential, usage, and refresh payloads;
4. Codex auth, JWT, usage, and refresh payloads;
5. missing, extra, mistyped, and null fields;
6. strict versus coercing behavior;
7. expiry-unit migration;
8. error paths shown by normal commands and doctor;
9. CLI startup time;
10. wheel and Homebrew packaging;
11. Python 3.14 on Linux, macOS, and Windows;
12. transitive dependencies, licenses, advisories, provenance, and release
    process.

Current recommendation:

- prefer Pydantic `TypeAdapter` when it delivers materially better strict
  validation and actionable paths without unacceptable packaging cost;
- prefer cattrs when it provides the cleanest dataclass boundary while meeting
  diagnostic and migration requirements;
- prefer msgspec when measured startup, decoding, or distribution behavior
  materially benefits the application;
- build focused parsing only when the mature candidates fail concrete
  requirements or cost more to integrate than the owned surface.

Whichever option wins stays at provider and persistence edges. Core models do
not inherit a validation framework merely to simplify external parsing.

This validation decision does not approve `pydantic-settings`. Pydantic
`TypeAdapter` validates untrusted boundary data; `pydantic-settings` discovers
and merges configuration sources. They solve different problems and require
separate adoption evidence. Even if Pydantic wins schema validation,
`pydantic-settings` remains deferred until the application has the cohesive
multi-source settings contract defined in section 3.10.

## 7. Provider integration packages

The package boundary is justified by current capability breadth:

- the 501-line Claude module combines credential discovery, parsing, usage
  routes, refresh, and setup-token behavior, while its concrete heartbeat
  adapter adds approximately 125 lines elsewhere;
- the 587-line Codex module combines credential detection, usage, refresh,
  Codex-home behavior, auth-file persistence, JWT parsing, and response
  parsing, while its concrete heartbeat adapter adds approximately 187 lines
  elsewhere.

Provider packages are symmetrical at the capability boundary, not forced into
identical leaf files.

Provider adapters receive the application wall clock when a relative
`expires_in` value must become an absolute expiry. They read the clock once,
produce an aware UTC `datetime`, and stop provider-native units at the adapter
boundary. They do not call `datetime.now()` or `time.time()` directly after the
clock migration.

### 7.1 Shared provider contract

`providers/base.py` contains only capabilities shared by every usage provider.

The current generic `run_setup_token()` method is removed because setup-token
is a Claude capability. Codex must not implement a method merely to raise an
unsupported-operation error.

The shared provider surface covers:

- credential detection where supported by the provider contract;
- usage fetching;
- typed token refresh behavior;
- stable provider identity and display metadata.

Refresh returns typed refreshed credentials or a typed outcome. It does not
return a Boolean while mutating hidden state.

### 7.2 Claude package

```text
providers/claude/
├── __init__.py
├── provider.py
├── credentials.py
├── usage.py
├── heartbeat.py
└── schemas.py
```

Responsibilities:

- `provider.py` composes the Claude provider facade and refresh behavior;
- `credentials.py` owns platform-specific credential discovery and parsing;
- `usage.py` owns usage routes, scope rules, request construction, and response
  conversion;
- `heartbeat.py` implements the heartbeat port for Claude;
- `schemas.py` owns untrusted Claude payload shapes and normalizes
  provider-native expiry and time units to aware UTC `datetime` values before
  constructing core models.

Interactive terminal input does not live in the provider package.

### 7.3 Codex package

```text
providers/codex/
├── __init__.py
├── provider.py
├── auth.py
├── usage.py
├── heartbeat.py
└── schemas.py
```

Responsibilities:

- `provider.py` composes the Codex provider facade and refresh behavior;
- `auth.py` owns `CODEX_HOME`, `auth.json`, JWT claims, identity matching,
  imports, exports, isolated per-account auth copies, and Codex-native
  timestamp formatting;
- `usage.py` owns usage requests and response conversion;
- `heartbeat.py` implements the heartbeat port for Codex;
- `schemas.py` owns untrusted Codex payload shapes and normalizes
  provider-native expiry and time units to aware UTC `datetime` values before
  constructing core models.

The application must never overwrite the user's active Codex login while
performing saved-account maintenance.

### 7.4 Registries

`providers/registry.py` explicitly constructs provider instances.

The heartbeat service consumes a map of `HeartbeatProvider` ports. Concrete
Claude and Codex heartbeat adapters live under their provider packages and are
wired by composition.

There is no dynamic plugin loader. Internal consumers import concrete modules
directly rather than relying on a broad package barrel.

## 8. CLI package and command hierarchy

### 8.1 Public package facade

The installed entry point remains:

```toml
sidekick-usages = "sidekick_usages.cli:app"
```

`cli/__init__.py` re-exports only:

```python
from sidekick_usages.cli.app import app, run

__all__ = ["app", "run"]
```

`__main__.py` imports the public `run` function so
`python -m sidekick_usages` continues to work.

Do not re-export private helpers, imported standard-library modules, service
classes, or constants solely to preserve implementation-coupled tests.

### 8.2 Composition root

`cli/app.py`:

- constructs the root Typer application;
- defines the root callback and version option;
- explicitly registers command modules;
- lazily composes executable-command dependencies;
- exports the application and process wrapper;
- contains no provider JSON, account persistence, or credential-file logic.

Conceptual registration:

```python
def create_app() -> BrandedTyper:
    """Build the sidekick-usages command tree."""
    app = BrandedTyper(...)
    register_usage_commands(app)
    register_account_commands(app)
    register_credential_commands(app)
    register_heartbeat_commands(app)
    register_maintenance_commands(app)
    register_doctor_commands(app)
    register_daemon_commands(app)
    register_update_commands(app)
    register_claude_commands(app)
    register_codex_commands(app)
    return app
```

Each registration function is explicit. Command modules never import the
global application. Each command module exposes one explicit registration
function. There is no import-by-string discovery, entry-point discovery,
command-registry dictionary, or generic command base class.

`create_app()` builds command structure only. A separate invocation-scoped
composition function:

1. discovers `ApplicationPaths`;
2. constructs `SystemClock`;
3. creates the pooled `HttpClient` and owns its closure;
4. constructs persistence, provider, scheduler, and credential adapters;
5. injects the clock, concrete paths, and adapters into application services;
6. returns the command-facing context; and
7. closes lifecycle-owned resources at process exit.

Help and version paths do not call runtime composition. They do not discover
application paths, load an account store, inspect credentials, create
directories, construct a scheduler backend, or initialize HTTP pools.

### 8.3 Application context

`cli/context.py` owns a frozen, command-facing context. Its final fields follow
concrete command needs; the representative shape is:

```python
@dataclass(frozen=True, slots=True)
class AppContext:
    account_store: AccountStore
    usage: UsageCheckService
    credentials: CredentialService
    heartbeat: HeartbeatService
    maintenance: TokenMaintenanceService
    doctor: DoctorService
    daemon: DaemonManager
    console: Console
    error_console: Console
```

Direct `AccountStore` access remains only if the simple account CRUD commands
do not justify a separate service. Add no field without a concrete command
consumer. Provider-specific commands use the credential service or a narrow
provider facade proven by caller analysis; they do not receive an entire
registry for convenience.

`ApplicationPaths`, `Clock`, `HttpClient`, raw provider registries, and
`SchedulerBackend` are composition inputs, not command-facing context fields.
They are injected into the services or facades that own their use. The context
is not a generic lookup service and contains no per-run result state.

These values do not belong in `AppContext`:

- provider filter;
- collected reports;
- failures;
- partial output;
- command-local flags;
- application path discovery;
- wall-clock or monotonic-time sources; and
- raw transport or retry-library objects.

Typer and Click carry the context through `ctx.obj`. Tests pass a typed context
to `CliRunner.invoke()` rather than mutating a module singleton.

`cli/context.py` is private application composition infrastructure. It is not
exported as a general service API.

An explicitly empty provider registry remains empty. Only `None`, where
permitted, selects a default.

### 8.4 Help adapter

The current `cli_help.py` is not a command. It customizes Typer and Click help
formatting and terminal width.

Its destination is:

```text
cli/help.py
```

`cli/help.py`:

- owns branded Typer command and group classes;
- owns the shared help-width policy;
- imports branding and UI frameworks only;
- never imports `cli/context.py`;
- never loads accounts, credentials, providers, or network clients.

The heartbeat label-fallback group is command-specific argument behavior and
moves to `cli/commands/heartbeat.py`.

A `cli/commands/help.py` file is created only if the product gains a real
`help <topic>` command.

### 8.5 Command ownership

Provider-neutral commands remain grouped by workflow:

| Module | Commands |
|---|---|
| `usage.py` | default invocation and `check` |
| `accounts.py` | `list`, `remove`, `rename`, `set-plan`, `reset` |
| `credentials.py` | provider-neutral `add` and `refresh` |
| `heartbeat.py` | heartbeat group and label fallback |
| `maintenance.py` | `maintain` and saved-token refresh |
| `doctor.py` | `doctor` |
| `daemon.py` | daemon install, status, and uninstall |
| `updates.py` | `check-update` and `update` |

Provider-specific command modules own actual provider capabilities:

```text
cli/commands/claude.py
cli/commands/codex.py
```

Preferred hierarchy:

```text
sidekick-usages claude setup-token
sidekick-usages codex login
sidekick-usages codex export
```

The current top-level command spellings may remain only where demonstrated
backward compatibility requires them, as deprecated aliases for a defined
compatibility period:

```text
sidekick-usages setup-token claude
sidekick-usages codex-login
sidekick-usages codex-export
```

Compatibility commands are thin delegates to the same application services.
They do not duplicate command workflows or provider operations, and they are
removed when the approved deprecation period ends.

Let the first release containing the provider command hierarchy be release
`R`. Deprecated aliases ship in `R`, remain available through the next minor
release, and are removed in the following minor release. The changelog and help
mark them deprecated in `R`; deprecation messaging never contaminates JSON,
quiet, scheduled, or other machine-readable stdout.

## 9. Application services

### 9.1 Usage checking

`usage/service.py` owns selection, usage collection, refresh-and-retry policy,
and failure aggregation.

```python
@dataclass(frozen=True, slots=True)
class UsageCheckResult:
    usages: tuple[AccountUsage, ...]
    failures: tuple[FetchFailure, ...]


class UsageCheckService:
    def check(
        self,
        provider_id: ProviderId | None,
    ) -> UsageCheckResult:
        ...
```

The exact model names are confirmed against the implementation before coding.

The service:

- returns data;
- does not print;
- does not raise `typer.Exit`;
- does not construct Rich renderables;
- preserves partial success;
- uses shared expiry and refresh policy;
- persists successful credential changes explicitly.

`FetchFailure` moves out of rendering because it is an application result, not
a presentation type.

### 9.2 Credential service

`credentials/service.py` coordinates provider-neutral credential workflows:

- detecting current local credentials;
- matching provider identity;
- applying detected credentials;
- refusing unintended identity replacement;
- requesting provider auth import or export operations;
- persisting an updated account;
- preserving the active-login safety rule.

Provider-specific file and protocol details remain in provider packages.
Terminal prompts remain in CLI adapters.

Credential states distinguish:

- missing;
- unreadable;
- malformed;
- incomplete;
- expired;
- rejected;
- identity mismatch;
- unsupported.

### 9.3 Expiry use in application services

`core/expiry.py` is the single owner of provider-neutral expiry
classification. Each application service obtains one aware UTC `now` value
from its injected `Clock` and passes the value into core policy. Services do
not read provider-native epoch units or compare formatted timestamp strings.

Maintenance alone derives "due for refresh" from a valid expiry and its
provider-specific refresh margin. The margin is maintenance use-case policy;
it is neither a persisted expiry state nor a core discriminant. Usage checking,
doctor, and heartbeat consume the core classification without inheriting
maintenance-specific thresholds.

### 9.4 Account persistence

`persistence/account_store.py` owns:

- validated loading;
- known-schema migrations;
- account querying;
- durable account persistence;
- atomic writes where supported;
- explicit persistence errors.

`persistence/migrations.py` owns both explicitly supported stored-schema
migrations and the durable-state location-migration coordinator defined in
section 4.2. It does not discover paths, parse provider-native homes, or own
Codex auth schemas. It consumes the injected `PrivateAuthMigrator` port and
never imports a provider package.

The common operation is explicit:

```python
def persist(self, account: Account) -> None:
    """Insert or update an account and durably save the store."""
```

The repeated `upsert(account)` plus `save()` sequence is removed from callers.

Public mutation semantics must be consistent. Internal in-memory helpers are
named as such and are not exposed as ambiguous durable operations.

Migration either succeeds and writes valid state, or raises a typed error.
Malformed data never becomes an apparently valid empty store.

## 10. Shared HTTP infrastructure

### 10.1 Ownership and current evidence

Sidekick Usages already reuses one `HttpClient`. Runtime composition injects it
into Claude, Codex, heartbeat, update, doctor, and other concrete consumers;
the command-facing `AppContext` does not expose the raw client. The evidence
commit contains nine direct production request call sites and four concrete
request capabilities:

- GET and decode JSON;
- POST JSON and decode JSON;
- POST form data and decode JSON;
- POST JSON and return response headers.

The problem is not a missing shared client. The current 471-line `http.py`
contains three retry loops, unvalidated JSON casts, integer-only
`Retry-After`, blanket POST retry behavior, and no shared connection pool.

HTTP does not belong in `core/`. Networking, TLS, proxies, sleeping, status
classification, dependency integration, and transport errors are
infrastructure concerns. `core/` remains independent of them.

Replace `http.py` atomically with:

```text
http/
├── __init__.py
├── client.py
└── retry.py
```

`http/__init__.py` preserves:

```python
from sidekick_usages.http import HttpClient
```

It exports only the stable Sidekick Usages HTTP façade and any concrete public
configuration type that proves necessary. Provider and feature modules never
import the selected transport, retry library, or their exceptions.

`http/client.py` owns:

- transport lifecycle and connection pools;
- HTTPS-only enforcement;
- the four concrete request capabilities;
- request and response size bounds;
- `HTTPMethod` request construction;
- generic JSON-object validation;
- response-header normalization;
- typed application-error translation;
- credential-safe diagnostics.

`http/retry.py` is the mandatory, cohesive owner of:

- closed operation retry policies;
- method and status eligibility;
- attempt and elapsed-time bounds;
- standards-compliant `Retry-After` handling;
- bounded jitter behavior;
- selected-library configuration.

The three concrete retry loops justify this responsibility. Do not add a
generic resilience framework, circuit breaker, arbitrary retry hook system,
request-class hierarchy, or empty HTTP model and port modules.

### 10.2 Client and dependency direction

```text
cli/context.py
    |
    v
http/HttpClient
    |
    +--> one selected transport and retry owner

providers/* ------> HttpClient <------ heartbeat/*
update.py --------> HttpClient

core/  X---------> http/
```

The existing concrete `HttpClient` dependency remains acceptable while there
is one production implementation. Do not add a second generic transport
protocol solely to hide the class. A protocol becomes justified if multiple
real implementations require it and typed test fakes cannot remain simple.

The HTTP package may import the recursive JSON vocabulary and application
error types. It imports no provider, CLI, Rich, Typer, persistence, account, or
renderer code.

### 10.3 Build-versus-adopt decision

Current primary-source research, retrieved on 2026-07-09, compared:

| Option | Current fit | Cost or risk | Assessment |
|---|---|---|---|
| Standard library plus focused executor | Adds no dependency | No shared connection pool; continues transport ownership | Measurement baseline only; cannot win |
| urllib3 2.7.0 plus urllib3 `Retry` | Pooling, TLS, timeouts, method policy, statuses, `Retry-After`, backoff, and jitter in one dependency | Transport migration must preserve typed behavior | Preliminary winner |
| urllib3 plus Tenacity 9.1.4 | Pooling and richer typed hooks | Two retry-capable layers can multiply attempts | Strong fallback with urllib3 retries disabled |
| urllib3 plus focused executor | Pooling with complete local retry control | Continues retry-algorithm ownership | Owned-code fallback with urllib3 retries disabled |
| HTTPX 0.28.1 plus Tenacity | Rich sync/async client, pooling, mock transport, optional HTTP/2 | Larger transitive and migration surface; advanced retries still external | No current justification |
| Stamina 26.1.0 | Active, typed, instrumented Tenacity wrapper | Adds a wrapper without owning HTTP semantics | Reject for current scope |
| `litl/backoff` 2.2.1 | Familiar retry decorator | Canonical repository is archived | Reject |

urllib3 `PoolManager` is the selected pooled transport boundary. urllib3's
`Retry` is the preliminary retry-owner leader because it addresses the complete
current HTTP problem with one actively maintained Python 3.14 dependency.
Tenacity is the leading retry-owner fallback if the spike proves that concrete
hooks or typed dynamic wait behavior cannot be expressed cleanly with urllib3
`Retry`. A focused local executor is the last fallback.

No dependency is approved until the focused spike in section 10.7 passes.
Exactly one layer owns retry:

- if urllib3 owns retry, no manual or Tenacity loop wraps it;
- if Tenacity or focused local code owns retry, urllib3 transport retries are
  disabled;
- provider adapters never construct retry objects or policies.

Pooling is a hard product requirement. The non-pooled standard-library option
remains a measurement baseline but cannot be the selected architecture. If
urllib3 fails transport, packaging, security, or platform acceptance, reopen
the transport decision rather than silently selecting a non-pooled fallback.

Bare Tenacity `@retry` is prohibited. Its documented default retries broad
exceptions indefinitely without waiting and is not a product policy.

Do not adopt HTTPX merely for hypothetical async or HTTP/2 needs. Reconsider it
only when concrete product behavior requires capabilities that the selected
sync client cannot supply.

### 10.4 Retry semantics

Retry is an operation-safety decision, not a generic HTTP-method loop.

RFC 9110 says clients should not automatically retry non-idempotent methods
unless they know the operation is effectively idempotent or know the first
request was not applied. The current client retries every POST on network
errors, 429, and 5xx; that behavior is not automatically preserved.

Audit each current POST against provider source or primary documentation:

- Claude inference-header probe;
- Claude credential refresh;
- Codex credential refresh;
- Claude heartbeat warming;
- Codex heartbeat warming.

A POST retry requires a concrete basis:

- documented idempotent resource semantics;
- a provider-supported idempotency key;
- an authoritative response proving the operation was not applied; or
- transport evidence proving no request was applied.

Use closed internal policies based on actual operations, such as:

- safe read;
- safe provider probe;
- approved credential exchange;
- no retry.

Do not expose `retry: bool`, arbitrary policy injection, or provider-created
retry configuration.

The shared retry policy includes:

- a bounded total attempt count;
- a bounded total elapsed-time deadline;
- separate per-attempt connect and read timeouts;
- selected network failures;
- HTTP 429;
- selected 5xx values when the operation is safe;
- full-jitter bounded backoff without server guidance;
- a capped valid `Retry-After` delay;
- one final typed outcome.

Do not retry:

- 401 with the same credentials;
- 403 with the same credentials;
- permanent 4xx responses;
- non-HTTPS or invalid URLs;
- malformed JSON;
- provider schema violations;
- local credential or persistence failures;
- programmer errors.

Provider-level token refresh after 401 is a separate application workflow. It
must not be hidden as an HTTP transport retry.

RFC 9110 permits `Retry-After` as either an HTTP date or non-negative delay
seconds. Accept both forms. Past dates become zero; malformed values select the
normal bounded jitter policy; excessive values are capped. The final valid
delay remains available on `RateLimitError` for user guidance.

### 10.5 Response and error contract

Successful HTTP client calls return only:

- a runtime-validated JSON object;
- normalized response headers.

Failures raise typed application exceptions. Errors are never returned as
values.

A syntactically valid JSON list, scalar, or `null` is not a valid JSON-object
response and becomes a typed boundary failure. Provider adapters subsequently
validate their provider-specific schemas.

Preserve:

- `AuthError` for 401;
- `ForbiddenError` for 403 with safely parsed diagnostic fields;
- `RateLimitError` for exhausted 429, including the last valid retry delay;
- `TransientError` for exhausted approved transport or 5xx failures;
- `InsecureUrlError` for a non-HTTPS or otherwise forbidden URL scheme;
- a typed invalid-payload error for malformed JSON or wrong top-level shape.

No urllib3, Tenacity, HTTPX, Stamina, or stdlib transport exception crosses the
package boundary. Error bodies are bounded and parsed defensively. Tokens,
authorization headers, credential payloads, and full account identities never
appear in an error representation.

### 10.6 Lifecycle and observation

Create one pooled client per CLI invocation. Register deterministic closure at
the composition boundary through a context manager or Click/Typer close
callback. Help and version paths must not initialize the client.

Tests inject a fake transport or constructed client; they do not patch a
library-global request function.

When retry observation is concretely consumed, record only:

- attempt number;
- non-sensitive operation and provider category;
- status or typed failure category;
- selected delay and whether it came from `Retry-After`;
- retrying versus terminal state;
- elapsed retry budget.

Retry observation never writes tokens, payloads, authorization headers, full
account identities, or unredacted provider bodies. It never changes JSON,
quiet, scheduled, or normal stdout behavior. Do not add an observer hook until
a real consumer exists.

### 10.7 Dependency spike and acceptance gate

Before production extraction, persist a focused comparison of:

1. urllib3 `PoolManager` plus urllib3 `Retry`;
2. urllib3 `PoolManager` plus Tenacity with urllib3 retries disabled;
3. urllib3 `PoolManager` plus a focused local executor with urllib3 retries
   disabled.

Use the real four request shapes and current typed error contract. Record:

- source sketches and owned line counts;
- exact attempt and exhaustion behavior;
- final 429 response and `Retry-After` access;
- integer and HTTP-date `Retry-After` behavior;
- safe and unsafe POST behavior;
- deterministic test ergonomics;
- connect, read, and total deadline behavior;
- proxy, CA, TLS, redirect, and header differences;
- CLI startup and multi-account request timing;
- wheel, lockfile, and Homebrew impact;
- Python 3.14 behavior on Linux, macOS, Windows, and WSL;
- license, advisories, provenance, maintenance, and release posture;
- the final selection and reversal conditions.

urllib3 wins only if it produces fewer and clearer owned lines, preserves typed
application failures, retains final rate-limit metadata, and passes packaging
and platform checks. Tenacity wins if concrete hook or typed-state needs make
urllib3 integration depend on brittle subclassing or obscure exhaustion state.
Focused local code wins only if both mature candidates fail concrete needs or
cost more to integrate and maintain than the cohesive owned surface.

The load-bearing HTTP tests cover:

- non-HTTPS input rejected as `InsecureUrlError` before transport access;
- valid object JSON;
- valid non-object JSON as a typed failure;
- malformed JSON as a typed failure;
- oversized response rejection through a bounded read;
- immediate 401 and 403 behavior;
- bounded 429 and selected 5xx retries;
- final rate-limit metadata;
- safe versus unsafe POST after ambiguous failure;
- both standard `Retry-After` forms and the configured cap;
- an injected monotonic time source and sleeper proving the total deadline
  stops retry while attempt budget remains;
- transport exhaustion translated into application errors;
- pool closure at the CLI lifecycle boundary;
- credential and authorization values absent from errors and observations;
- unchanged JSON, quiet, and scheduled output.

Tests assert Sidekick Usages behavior, not library internals.

All decision-relevant HTTP research, source facts, option comparisons,
recommendations, risks, and reversal conditions are inlined in sections 10 and
20. The implementation spike must inline its measured results and final
decision in this specification before dependency approval. It may also preserve
supplementary evidence in the git-tracked section 3.9 layout.

## 11. Presentation contract

Presentation uses a small, explicit contract:

- human builders return Rich `RenderableType`;
- machine builders return typed JSON data or one documented serialized form;
- command adapters select human or machine mode;
- command adapters own stdout and stderr;
- services never print;
- renderers never call providers or persistence;
- renderers receive an explicit reference time or a completed read model and
  never call `datetime.now()` directly;
- branding remains a shared presentation primitive;
- help remains independent of application initialization.

JSON, quiet, scheduler, and version output remain undecorated.

`usage/render.py` receives complete data and builds the approved overview. Its
physical move from `render.py` does not authorize a visual redesign.

The current 739-line `render.py` stays intact until the usage package and
service create the concrete ownership boundary for that move. At that point it
may move atomically to `usage/render.py`; it is not mechanically divided into
smaller renderer files unless three stable rendering subdomains or another
clear responsibility boundary have emerged. File movement alone is not an
architectural improvement.

Do not add a generic screen, renderer-service, or hook framework.

## 12. Dependency direction

```text
sidekick_usages.cli
    |
    v
cli/app.py
    +----> paths.py ----> platformdirs
    +----> clock.py
    |
    | injects constructed adapters and services
    v
cli/commands/
    |                 \
    v                  v
application services  presentation
    |                  |
    +--------+---------+
             v
   core models/types/expiry
             ^
             |
     +-------+--------+
     |       |        |
providers persistence scheduler

heartbeat service --> heartbeat ports <-- provider heartbeat adapters
providers and heartbeat adapters --> http/HttpClient --> selected transport
```

Enforce these rules:

- `core/` imports no CLI, provider, persistence, HTTP, Rich, or scheduler code;
- `core/` imports no external settings loader, operating-system path
  discovery, filesystem, or infrastructure module;
- pure core policy accepts validated values and aware times explicitly;
- provider and persistence adapters import core models, never CLI commands;
- providers and heartbeat adapters consume the Sidekick Usages `HttpClient`,
  never a transport or retry dependency directly;
- `http/` imports no providers, CLI, Rich, Typer, persistence, accounts, or
  renderers;
- services import ports and core types, never Rich or Typer;
- `platformdirs` is imported only by `paths.py`;
- no core, service, command, provider, or persistence module reconstructs a
  Sidekick-owned root with `Path.home()`;
- `clock.py` provides wall time only; HTTP elapsed deadlines use the monotonic
  source owned by `http/retry.py`;
- boundary serializers depend on core datetimes, while core never depends on
  their wire, storage, or display formats;
- renderers import read models and branding, never adapters, and own human
  timestamp display formatting;
- CLI commands translate options into service calls and results into output;
- command modules never import the global application;
- provider packages may implement feature ports but never import feature
  services;
- package initializers remain thin compatibility surfaces.

## 13. Error and status model

The existing `UsageError` hierarchy remains the application error root.

Keep:

- `AuthError`;
- `ForbiddenError`;
- `RateLimitError`;
- `TransientError`;
- `UnsupportedOperationError` where a user can request an unsupported
  capability through a legitimate generic surface.

Add only errors required by concrete boundaries, such as:

- invalid or unreadable persisted account state;
- conflicting canonical and existing-Sidekick authoritative state;
- invalid credential file;
- unsupported stored schema;
- unsafe identity replacement;
- invalid provider payload;
- malformed or non-object HTTP JSON payload;
- insecure or forbidden HTTP URL scheme;
- unsafe retry policy selection where a concrete caller can request it.

Closed status vocabularies use enums where they already cross multiple modules:

- `DaemonOperation`;
- `RefreshStatus`;
- `ExpiryState`;
- `HeartbeatStatus`;
- `ExitCode`;
- `ProviderId`.

An unexpected daemon operation is rejected. It never maps to uninstall.

A parse, I/O, or collection failure never becomes:

- zero lifetime usage;
- zero remaining time;
- an empty credential state;
- an empty account store;
- a successful refresh.

## 14. Confirmed reuse decisions

The following repeated concepts have enough concrete consumers to centralize:

| Concept | Current repetition | Chosen owner |
|---|---|---|
| Provider usage scope | `PROFILE_SCOPE` and `_USAGE_REQUIRED_SCOPE` | Claude provider package |
| Provider account filtering | Existing `AccountStore.filter_by_provider()` plus CLI, doctor, maintenance, and heartbeat filters | Account store/query service |
| Application wall time | Maintenance, heartbeat, and doctor read current time | Injected `Clock` from `clock.py` |
| Persisted timestamp encoding | Refresh and heartbeat audit fields | `persistence/schemas.py` |
| Provider-native timestamp encoding | Codex `auth.json` and provider wire formats | Owning provider schema or auth module |
| Human timestamp display | Usage, heartbeat, and doctor output | Owning renderer |
| HTTP elapsed deadlines | Retry loops measure elapsed budgets | Monotonic source in `http/retry.py` |
| Credential result fields | `_CredentialFields` duplicates `DetectedCredentials` | `core/models.py` |
| Durable account update | Approximately 19 `upsert()` plus `save()` pairs | `AccountStore.persist()` |
| Exit-status reduction | Three command-specific reducers | Typed status policy |
| HTTP retry loop | Three similar request loops | `http/retry.py` behind the selected retry owner |
| Expiry classification | Check, maintenance, doctor, heartbeat | `core/expiry.py` |
| Refresh-outcome rendering | Multiple command loops | One presentation helper |
| Sidekick-owned application locations | Store, CLI, and lifetime reconstruct the root | `ApplicationPaths` in top-level `paths.py` |

The three current `_now_utc_z()` implementations are mechanically identical
but cross persistence and provider contracts. That is not one semantic
abstraction. Replace them with aware wall time plus boundary-owned encoding;
do not create a universal timestamp-string formatter.

Renderers receive an aware timestamp, an explicit reference time, or a
precomputed display value through their input/read model. They own human
formatting but never acquire current time themselves.

Do not hand-write a retry helper if the dependency spike selects mature library
machinery. If the focused local baseline wins, the executor remains private and
typed:

```python
def _with_retries[T](request: Callable[[], T]) -> T:
    ...
```

It preserves intentional typed behavior while applying the explicit POST,
deadline, `Retry-After`, and jitter fixes in section 10. It does not add policy
hooks, configurable strategies, or extension points without concrete callers.
The extraction does not become a generic utility module.

## 15. Migration and compatibility strategy

The migration is phased, but every phase must be production-quality and align
with the complete target.

HTTP compatibility preserves:

- the `sidekick_usages.http.HttpClient` import;
- the capability-oriented public method surface;
- successful return-value behavior; and
- the meanings of existing typed application exceptions.

Compatibility does not preserve defects. Blanket POST retry after ambiguous
failure, unchecked JSON casts, raw transport exceptions, and non-HTTPS
`ValueError` leakage are approved correctness changes. Non-HTTPS input raises
the typed `InsecureUrlError` so the normal application boundary handles it.

### 15.1 Safety and hygiene

First:

- make daemon operations exhaustive;
- preserve explicitly empty heartbeat registries;
- share help and masthead width policy;
- remove verified dead and no-op surfaces;
- sanitize fixture and documentation identifiers;
- remove unjustified suppressions;
- correct stale comments;
- fix the first focused line-length findings and enable their gates from a
  clean baseline.

These changes reduce risk before moving ownership.

### 15.2 Core, schemas, persistence, HTTP, and paths

Then:

- complete the validation-library decision;
- create top-level `paths.py` with frozen `ApplicationPaths`;
- centralize the exact current Sidekick-owned locations without relocating
  existing files;
- inject concrete paths from the current composition root (`cli.py` during
  intermediate phases, then `cli/app.py` after the atomic CLI package
  conversion) into their consuming adapters and services;
- complete the `platformdirs` dependency and cross-platform output spike;
- establish `core/models.py`, `core/types.py`, and `core/expiry.py`;
- establish `clock.py` and inject aware wall time into application services;
- move shared models without changing behavior;
- move provider-neutral expiry classification into core;
- replace shared timestamp strings with aware datetimes and boundary-local
  persistence, provider, and presentation encoding;
- establish JSON and boundary schemas;
- complete and persist the HTTP transport and retry dependency spike;
- atomically replace `http.py` with the shared `http/` package;
- establish one pooled client lifecycle and exactly one retry owner;
- make POST retry safety explicit per concrete operation;
- parse both standard `Retry-After` forms with a product cap;
- translate every transport failure into the application error vocabulary;
- create the persistence package;
- make migration failures explicit;
- add durable account persistence;
- normalize expiry representation.

Stored account compatibility is preserved through explicit schema migration,
not permissive defaulting.

Path centralization and path relocation are different changes. Initial
centralization preserves the documented `~/.config/sidekick-usages` behavior
and proves that consumers no longer reconstruct it. A later native-location
migration may activate `platformdirs` locations only after persistence/schema
migration is stable and the following cases have explicit behavior:

- only the prototype `cc-usage` location exists;
- only the existing Sidekick location exists;
- only the proposed canonical location exists;
- canonical and existing Sidekick locations both exist with equivalent state;
- canonical and existing Sidekick locations both exist with conflicting state;
- an authoritative location is malformed or unreadable;
- the prototype-only fallback is malformed or unreadable;
- a stale prototype coexists with a valid authoritative store without blocking
  it; and
- credential-bearing files have unsafe permissions.

The native-location migration uses atomic copy or write behavior where the
platform supports it, preserves required credential permissions, surfaces its
state through `doctor`, and never silently merges, overwrites, or deletes a
compatibility or prototype store. Disposable caches may be regenerated only
after their lifecycle classification is explicit. Schema migration and
filesystem-location migration do not run as one opaque operation.

`Account.codex_home` is persisted and may point either to a Sidekick-owned
private auth bundle or an external/source `CODEX_HOME`. Migration copies
private auth bundles first and rewrites only paths proven to be descendants of
`ApplicationPaths.private_codex.existing_sidekick`. The destination preserves
the relative path below `ApplicationPaths.private_codex.canonical`. It never
rewrites an external or provider-native home. The injected
`PrivateAuthMigrator` validates and copies bundles without creating a provider
import in persistence. Auth files retain owner-only permissions. Updated
account state is committed atomically only after every required copy and
validation succeeds; old data remains in place. Partial destinations and
conflicting bundles are typed failures visible in `doctor`.

This phase does not add `pydantic-settings`, a global settings singleton, or an
empty configuration package.

### 15.3 Usage service

Then:

- create `UsageCheckService` and its immutable result;
- remove usage-check state from `AppContext`;
- move collection and refresh retry out of the CLI;
- move private-helper tests to the service boundary;
- retain a small number of CLI integration tests.

### 15.4 Provider and credential ownership

Then:

- convert Claude and Codex modules into packages;
- move concrete heartbeat adapters under providers;
- extract Codex auth-file behavior;
- extract Claude credential-source behavior;
- centralize provider-neutral credential coordination;
- replace Boolean-plus-mutation refresh behavior.

Provider package initializers may temporarily re-export stable public classes.
Do not permanently re-export private parsers, constants, or imported modules.

### 15.5 CLI package

Then atomically replace:

```text
src/sidekick_usages/cli.py
```

with:

```text
src/sidekick_usages/cli/
```

The same change:

- creates `cli/__init__.py`, `app.py`, `context.py`, and `help.py`;
- moves complete command clusters;
- updates tests to patch the owning module or inject dependencies;
- preserves `sidekick_usages.cli:app`;
- preserves `python -m sidekick_usages`;
- verifies the built wheel contains no stale `cli.py`.

The provider command hierarchy and compatibility aliases are introduced from
one shared service implementation.

### 15.6 Presentation and final gates

Finally:

- normalize human and JSON presentation contracts;
- consolidate repeated outcome rendering;
- remove remaining boundary casts and `Any`;
- enforce annotation and suppression gates;
- add a mechanical module-size check;
- reassess cohesive modules only when they exceed the target or gain a clear
  new responsibility.

### 15.7 Dead and speculative surfaces

Revalidate callers immediately before editing, then remove these surfaces if
they remain unused:

- the no-op doctor `--auth` option and its discarded value;
- `diagnostic_dicts`;
- `providers.get_provider`;
- the unused `acct` parameters in
  `_should_retry_bodyless_forbidden` and `_refresh_credential_home`;
- the over-exported `heartbeat_status_dict`;
- stale comments attached to those paths.

Remove `Provider.run_setup_token` from the generic contract because only
Claude implements the capability. `brand_line(section)` has one production
caller, so use a specifically named update-status function until three real
branded-line consumers justify a parameterized abstraction.

Do not preserve dead exports, no-op options, or speculative parameters for
compatibility unless an actual supported external consumer is identified.

### 15.8 Known comment and docstring drift

Inspect and correct or delete:

- the CLI module comment describing an `_ctx` global that no longer exists;
- the `HttpClient` claim that it is GET-only;
- the provider comment claiming one provider requires one file;
- the numbered render comment labelled `# 6`;
- the package-level claim that every provider uses an OAuth usage endpoint;
- the lifetime comment referring to an obsolete task number;
- redundant `:returns:` fields on `-> None` commands.

Comments explain durable intent. They do not preserve an old implementation
plan.

### 15.9 Phase acceptance criteria

Each migration phase is independently reviewable and preserves behavior unless
this spec identifies a defect. A phase contains only tests that protect its
acceptance criteria.

#### Safety and hygiene

Acceptance requires:

- no unexpected daemon input can trigger uninstall;
- an explicitly empty heartbeat registry is preserved;
- help and masthead widths agree at narrow and wide terminals;
- no realistic credential or account identities remain in fixtures or
  examples;
- removed functions have no remaining callers;
- focused and full tests pass; and
- lint and type checks pass for touched modules.

#### Core, schemas, persistence, HTTP, paths, and type boundaries

Acceptance requires:

- the schema-validation evidence and decision are inlined here and satisfy
  every section 6.4 spike criterion before a validation dependency is approved;
- malformed store data cannot masquerade as an empty valid store;
- migration failure is observable and actionable;
- callers cannot accidentally update in memory without durable persistence;
- expiry parse and I/O failures cannot look like zero remaining time;
- no `Any` or unjustified cast crosses the new persistence boundary;
- no `Any` or unjustified cast crosses the new HTTP JSON boundary;
- core models remain independent of HTTP, persistence, and provider schemas;
- `core/expiry.py` owns only pure provider-neutral expiry policy;
- core imports no external settings loader or operating-system path discovery;
- application services pass one aware `now` value into each core expiry
  decision;
- persistence and provider timestamp encoders have separate behavioral tests;
- no production `timestamps.py` or universal timestamp-string formatter
  exists;
- stored-data compatibility is covered by a small load-bearing migration test
  set;
- `ApplicationPaths` is frozen and injected from the composition root;
- no import-time Sidekick path singleton or duplicate Sidekick-owned
  `Path.home()` reconstruction remains;
- path discovery creates no directory or file;
- initial path centralization preserves every current physical location;
- `platformdirs` is imported only by `paths.py`;
- no global settings object or unapproved settings-loader dependency exists;
- exactly one owner remains for Sidekick-managed durable-state, private-auth,
  and cache locations; and
- provider-native homes remain provider-owned while scheduler installation
  paths remain daemon-owned.

Native-location migration acceptance additionally requires:

- Linux, macOS, Windows, and WSL discovery behavior is verified;
- absence of every candidate store produces the documented empty initial
  state, not a migration success claim;
- prototype-only, existing-Sidekick-only, and canonical-only states have
  explicit typed outcomes;
- equivalent and conflicting canonical-plus-existing-Sidekick states have
  explicit typed outcomes;
- malformed or unreadable authoritative state fails closed;
- malformed or unreadable prototype state fails when it is the only candidate;
- a stale prototype does not block or overwrite a valid authoritative store;
- migration source, destination, conflict, and recovery action are visible
  through `doctor`;
- `doctor` can render the read-only migration assessment without a successfully
  loaded account store;
- `persistence/migrations.py` is the only durable-state location-migration
  coordinator, while `paths.py` remains side-effect-free discovery;
- equal private-Codex roots produce no relocation during the initial
  compatibility-preserving centralization;
- distinct private-Codex roots preserve each account's relative destination
  below the canonical root;
- descendant, external, already-canonical, and misleading shared-prefix
  `codex_home` values are classified correctly;
- persistence consumes the injected `PrivateAuthMigrator` port and imports no
  provider package;
- account and credential permissions are preserved;
- Sidekick-owned Codex auth bundles are copied before persisted paths change;
- external or provider-native `CODEX_HOME` values are never rewritten;
- account-state commit occurs only after every required auth copy validates;
- partial or conflicting auth destinations fail explicitly;
- durable state is never silently merged, overwritten, relocated, or deleted;
  and
- regenerable cache behavior is tested independently from durable state.

HTTP acceptance additionally requires:

- an inlined dependency decision with one selected retry owner;
- no stale `http.py` in the source tree or built wheel;
- one shared client whose pools close at the CLI lifecycle boundary;
- provider and feature code importing no transport or retry dependency;
- explicit safe versus unsafe POST behavior;
- both standard `Retry-After` forms and a bounded product cap;
- non-HTTPS rejection before any transport call;
- typed oversized-response failure from a bounded read;
- total-deadline exhaustion stopping retry while attempt budget remains;
- credential-safe error and observation output;
- no transport exception escaping the HTTP package; and
- focused behavioral tests for attempt count, waits, typed exhaustion, JSON
  shape, and output compatibility.

#### Usage application service

Acceptance requires:

- no Rich, Typer, or printing dependency in the service;
- command-owned output mode and exit mapping;
- explicit partial provider failure;
- stable documented human and JSON output; and
- no tests patching the former private collection and render helper.

#### Provider and credential ownership

Acceptance requires:

- no Boolean-plus-hidden-mutation refresh contract;
- accurate command and maintenance outcomes for credential errors;
- provider-specific parsing inside the owning provider package;
- no provider schema leakage into `core/` or CLI commands;
- no Typer or Rich renderer imports from provider packages;
- one canonical credential model; and
- concise coverage of the safety-critical states without real credentials.

#### CLI package and command hierarchy

Acceptance requires:

- no `cli.py` in the built package;
- `cli/app.py` at approximately 200 lines or fewer;
- a valid `sidekick_usages.cli:app` entry point;
- a functional `python -m sidekick_usages` path;
- help rendering without account or credential initialization;
- help and version perform no application-path discovery, store loading,
  directory creation, scheduler construction, or HTTP initialization;
- discoverable Claude and Codex capabilities;
- every command module below the 800-line target;
- no Typer import from providers or services;
- no Rich import from core or application services;
- stable command discovery and help output;
- compatibility behavior matching the approved migration policy; and
- a wheel containing the package with no stale `cli.py`.

#### Presentation consistency and final gates

Acceptance requires:

- human renderers returning Rich renderables;
- JSON paths following one typed serialization convention;
- human timestamp display remaining renderer-owned;
- no printing in services;
- no provider, persistence, or other I/O in renderers;
- quality gates passing without blanket suppression;
- every production module below 1000 lines; and
- no unused framework or extension surface.

### 15.10 Recommended change-set boundaries

Keep commits and reviews narrow. The expected sequence is:

1. `fix(cli): make daemon operations exhaustive`
2. `fix(heartbeat): preserve explicit empty registries`
3. `fix(help): share terminal width policy`
4. `chore: remove dead surfaces and stale suppressions`
5. `docs(research): decide schema validation dependency`
6. `docs(research): decide HTTP transport and retry dependency`
7. `docs(research): decide application path discovery dependency`
8. `refactor(http): centralize transport and retry policy`
9. `refactor(paths): inject current sidekick-owned paths`
10. `refactor(core): centralize models, types, and expiry policy`
11. `refactor(time): separate clocks from timestamp serialization`
12. `refactor(store): validate and persist accounts explicitly`
13. `feat(persistence): migrate native application data safely`
14. `refactor(usage): extract typed usage check service`
15. `refactor(providers): create claude and codex packages`
16. `refactor(credentials): centralize credential state`
17. `refactor(cli): create cli package and command groups`
18. `chore(quality): enforce architecture hygiene gates`

The native-location change set occurs only if its explicit migration gate
passes. It never combines with initial path injection or stored-schema
migration. Adjust a boundary only to keep the repository buildable. Do not
collapse unrelated phases into one large refactor.

## 16. Test design

### 16.1 Load-bearing behavior

Retain concise tests for:

- root, nested, and leaf help without application initialization;
- one masthead before `Usage:`;
- help and masthead width agreement;
- the existing 85-column overview floor and wide-terminal help policy;
- version output;
- JSON output;
- quiet and scheduled output;
- help and version without runtime composition;
- account listing and mutation behavior;
- daemon operation rejection and dispatch;
- frozen injected `ApplicationPaths` with no discovery side effects;
- compatibility-preserving current path resolution;
- no account-location candidate existing;
- equivalent and conflicting canonical-plus-existing-Sidekick stores;
- prototype-only import and stale prototype beside authoritative state;
- malformed or unreadable authoritative and prototype-only candidates;
- partially created path-migration destinations and private-auth collisions;
- equal and distinct private-Codex roots, including relative destination
  preservation;
- descendant, external, already-canonical, and misleading shared-prefix
  `codex_home` classification;
- account-file and auth-file permission preservation;
- `doctor` migration source, destination, conflict, and recovery output;
- Linux, macOS, Windows, and WSL native path discovery;
- `SystemClock.now()` returning aware UTC;
- deterministic expiry immediately before, at, and after its boundary;
- aware UTC wall-clock injection at expiry-policy boundaries;
- separate persistence and provider-native timestamp encoding;
- provider-native expiry units stopping at provider adapters;
- HTTP deadlines remaining monotonic while wall time moves;
- provider refresh retry;
- identity-preserving credential refresh;
- explicit identity replacement;
- persistence and migration failures;
- missing versus malformed credentials;
- maintenance ordering and exit status;
- heartbeat scheduling and provider targets;
- usage partial success and typed failures;
- provider command groups and compatibility aliases;
- canonical robot ownership and approved exact art.

### 16.2 Tests to move

Tests that call private CLI fetch helpers move to `UsageCheckService`.

In particular, tests that patch or call `_fetch_and_render` must exercise the
public usage service and command boundary after extraction.

`tests/test_cli_refresh.py` is approximately 787 lines at the evidence commit.
Split it only along the production command and service boundaries when that
makes failures clearer. Do not turn it into many setup-heavy one-assertion
files.

Tests that monkeypatch incidental imports in `sidekick_usages.cli` move to:

- typed dependency injection; or
- the module that actually consumes the dependency.

Do not add compatibility exports solely to keep old patch targets alive.

### 16.3 Tests to consolidate or remove

Remove or consolidate:

- a smoke test that only checks whether `app` exists;
- duplicate robot literals in the overview test when canonical-source coverage
  already protects the one-source rule;
- the assertion that the first stripped robot line is merely `"o"`;
- assertions for an absent historical sentence that no longer encodes a
  product rule;
- redundant exact-value and ordering checks covering the same outcome;
- duplicated caption/layout fixtures;
- tests that assert only exit code 1 when the expected error and unchanged
  state are the real behavior.

Also remove redundant assertions such as an exact duration result followed by
an ordering assertion already implied by those values. Merge caption cases
only when one parameterized behavior test remains clearer than separate tests.

Strengthen rate-limit and set-plan tests to assert typed outcomes and persisted
state. Set-plan coverage asserts the stored plan or emitted command, not merely
that a mock was called. Rate-limit coverage asserts retry count, delay or
policy, and final typed outcome without pinning incidental log wording.

### 16.4 Test fixtures

Use reserved synthetic identifiers such as:

```text
long.account.name@example.test
```

Never use real account labels, provider identifiers, OAuth material, or files
from the user's configuration.

Fakes remain small and local unless three concrete test modules need the same
typed harness. Avoid autouse fixtures and hidden global mutation.

Seven test modules currently duplicate `AppContext` construction. After the
production context stabilizes, replace that repetition with one small,
explicit, typed harness. It must not be autouse, mutate global state, build
every dependency for every test, or become a general fake framework.

Type `pytest.MonkeyPatch`, `Path`, fake methods, and helper return values. Keep
filesystem, provider, HTTP, wall-clock, monotonic-time, and scheduler
boundaries injectable. The fake application wall clock and HTTP monotonic timer
remain separate unless a concrete shared semantic contract emerges.
Do not introduce `Any` merely to make a fixture, mock, or helper convenient.

### 16.5 Architecture checks

Add focused checks for:

- no production module over 1000 lines;
- review warning near 800 lines;
- no `Any` or unjustified casts in application, domain, or test code;
- no blanket suppressions;
- no CLI imports from core or providers back into commands;
- no Rich or Typer imports in services and core;
- no transport or retry-library imports outside `http/`;
- exactly one configured HTTP retry owner;
- no HTTP, external settings loader, operating-system path discovery,
  filesystem, or infrastructure import from `core/`;
- `core/expiry.py` remains pure and infrastructure-independent;
- no production `timestamps.py` or universal timestamp-string formatter;
- no application-wide settings singleton;
- no declared runtime `pydantic-settings` dependency or production import
  without a separately approved settings contract;
- no `platformdirs` import outside `paths.py`;
- no provider-native or scheduler installation path in `ApplicationPaths`;
- no `ApplicationPaths`, wall-clock, raw HTTP client, provider registry, or raw
  scheduler backend in `AppContext`;
- no duplicate Sidekick-owned `Path.home()` reconstruction;
- no import-time Sidekick path discovery;
- no durable-state location-migration coordinator outside
  `persistence/migrations.py`;
- no provider-package import from `persistence/migrations.py`;
- the location-migration coordinator consumes the narrow
  `PrivateAuthMigrator` port instead of provider auth internals;
- no private-Codex root reconstruction outside `paths.py`;
- no direct `datetime.now()` or `time.time()` in application services,
  providers, or renderers after clock migration;
- no clock import from `core/expiry.py`;
- no provider and persistence timestamp serializer importing the other;
- exactly one `ApplicationPaths` owner for Sidekick-owned locations;
- exactly one canonical robot source;
- built-wheel CLI package contents;
- no stale same-named module after package conversions.

## 17. Quality gates

The completed migration must pass:

```bash
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files
npm run lint:markdown
uv build
```

Introduce new mechanical rules from a clean baseline:

1. fix the 15 current `E501` findings, then enable the rule;
2. fix the 8 current `W505` findings and enforce the 79-character document
   line limit;
3. remove the 10 current `# noqa` comments by fixing their underlying findings
   or documenting one unavoidable rule-specific constraint;
4. remove the unused global `B905` ignore;
5. eliminate `Any` and unjustified casts at extracted boundaries;
6. enable focused `ANN401` or equivalent enforcement once clean;
7. enforce the 1000-line hard limit and an approximately 800-line review
   warning;
8. use native Python 3.14 deferred annotations consistently, remove the legacy
   stringizing future import, and keep `AGENTS.md` aligned with the enforced
   `pyupgrade --py314-plus` gate;
9. reject blanket suppressions.

Do not enable a broad rule set and suppress its findings.
Clean one coherent class of findings and enable its gate in the same change.

## 18. Security and operational constraints

- Never commit access tokens, refresh tokens, ID tokens, or account exports.
- Never log credential values or full provider identity values.
- Keep credential fields out of object representations.
- Require HTTPS for provider traffic.
- Preserve TLS verification and verify proxy and CA behavior on every supported
  platform when changing HTTP transports.
- Bound response bodies, per-attempt timeouts, total retry time, attempts, and
  server-directed waits.
- Never retry a credential-bearing POST without a documented operation-safety
  basis.
- Redact provider failures before persistence or display.
- Never let saved-account maintenance adopt or overwrite the active CLI login.
- Distinguish source login homes from sidekick-owned isolated auth copies.
- Preserve file permissions on credential and account data.
- Use atomic writes where supported.
- Never silently relocate, merge, overwrite, or delete account or credential
  state during path migration.
- Copy and validate Sidekick-owned private auth bundles before atomically
  updating persisted paths; never rewrite external `CODEX_HOME` values.
- Surface partial, conflicting, malformed, and unreadable migration state
  through typed errors and `doctor`.
- Treat durable credential-bearing data separately from regenerable cache.
- Keep JSON and quiet output stable for automation.
- Preserve Linux, macOS, Windows, and WSL behavior.
- Validate the wheel and Homebrew path after package moves.

## 19. Implementation operating procedure

Before every implementation phase:

1. confirm the exact repository and branch;
2. fetch or pull as appropriate and record the audited base;
3. inspect the worktree and preserve unrelated user changes;
4. search exact concept names before adding types or helpers;
5. read two or three neighboring files;
6. refresh caller, line-count, and gate evidence because this spec records a
   point-in-time snapshot;
7. complete and persist required build-versus-adopt research;
8. write a narrow phase plan with explicit acceptance criteria;
9. implement with `apply_patch` and keep patches reviewable;
10. run the smallest relevant tests first;
11. run all repository gates before committing;
12. inspect the diff for behavior drift, dead code, comment drift, credentials,
    suppressions, and abstractions with fewer than three callers; and
13. commit conventionally and push only when explicitly requested.

Use the documented repository gates:

```bash
uv sync --all-groups
uv run pytest --cov=sidekick_usages
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pre-commit run --all-files
npm run lint:markdown
uv build
```

Also inspect the repository directly:

```bash
git diff --check
git status --short
rg -n "\bAny\b|cast\(" src tests
rg -n "# noqa|# type: ignore|# nosec" src tests
rg -n "except\s*:\s*(pass)?|except Exception" src tests
```

Aggregate success is not a substitute for these focused checks.
During iteration, run the smallest relevant test module before the full suite.

## 20. Research sources

The organizational distinctions and dependency assessment use current primary
sources retrieved on 2026-07-09:

- **Repository commit:**
  `42cd01eb17c7903b385b1b4e259cf5b0c64126c5`
- **Python target:** 3.14
- **Current relevant runtime dependencies:** Click, Typer, and Rich
- **Validation baseline:** no current runtime validation dependency
- **Open decision:** schema-validation selection requires the repository spike
  in section 6.4
- **HTTP baseline:** no current third-party HTTP or retry dependency
- **Open HTTP decision:** urllib3 `PoolManager` is the preliminary pooled
  transport; section 10.7 selects and approves the single retry owner
- **Application-path baseline:** `store.py`, `cli.py`, and `lifetime.py`
  duplicate Sidekick-owned location construction; `store.py` also retains the
  separate `cc-usage` prototype source
- **Path dependency baseline:** `platformdirs` is not a declared runtime
  dependency; version 4.9.6 appears only transitively in the development lock
  graph through `python-discovery` and `virtualenv`
- **Application-path decision:** `platformdirs` 4.10.0 is the preliminary
  direct runtime adoption choice behind `paths.py`, subject to the explicit
  cross-platform and migration gate in section 4.2
- **Settings baseline:** the application has no cohesive multi-source settings
  contract
- **Settings decision:** `pydantic-settings` 2.14.2 is deferred and is not
  implied by a Pydantic schema-validation decision

Local path evidence at the evidence commit is:

- `store.py` defines the current Sidekick account path and distinct prototype
  migration path;
- `cli.py` derives the Sidekick-owned private Codex root;
- `lifetime.py` independently reconstructs the Sidekick cache root;
- `providers/codex.py` separately owns provider-native `CODEX_HOME` discovery
  and persists Sidekick-owned private auth locations on accounts; and
- `daemon.py` separately owns scheduler installation paths.

- [Python modules and packages][python-packages]
- [Python Packaging User Guide: `src` layout][python-src-layout]
- [Python typing reference][python-typing]
- [Python dataclasses][python-dataclasses]
- [Cockburn hexagonal architecture][cockburn-hexagonal]
- [AWS hexagonal architecture guidance][aws-hexagonal]
- [Microsoft DDD dependency guidance][microsoft-ddd]
- [Architecture Patterns with Python composition root][cosmic-composition]
- [Twelve-Factor configuration semantics][twelve-factor-config]
- [Typer commands][typer-commands]
- [Typer command help][typer-help]
- [Typer subcommand applications][typer-subcommands]
- [Typer application reference][typer-reference]
- [Pydantic models][pydantic-models]
- [Pydantic TypeAdapter][pydantic-adapter]
- [Pydantic dataclasses][pydantic-dataclasses]
- [Pydantic package metadata][pydantic-pypi]
- [Pydantic canonical repository][pydantic-github]
- [Pydantic settings documentation][pydantic-settings-docs]
- [pydantic-settings package metadata][pydantic-settings-pypi]
- [cattrs documentation][cattrs-docs]
- [cattrs package metadata][cattrs-pypi]
- [cattrs canonical repository][cattrs-github]
- [msgspec documentation][msgspec-docs]
- [msgspec package metadata][msgspec-pypi]
- [msgspec canonical repository][msgspec-github]
- [RFC 9110 HTTP semantics][rfc-9110]
- [RFC 6585 status codes][rfc-6585]
- [AWS bounded retry guidance][aws-retries]
- [AWS exponential backoff and jitter][aws-jitter]
- [urllib3 retry documentation][urllib3-retry]
- [urllib3 package metadata][urllib3-pypi]
- [urllib3 canonical repository][urllib3-github]
- [Tenacity documentation][tenacity-docs]
- [Tenacity API reference][tenacity-api]
- [Tenacity package metadata][tenacity-pypi]
- [Tenacity canonical repository][tenacity-github]
- [HTTPX transport documentation][httpx-transports]
- [HTTPX package metadata][httpx-pypi]
- [Stamina package metadata][stamina-pypi]
- [backoff canonical repository][backoff-github]
- [platformdirs API][platformdirs-api]
- [platformdirs platform behavior][platformdirs-platforms]
- [platformdirs package metadata][platformdirs-pypi]

Package metadata at the research date listed:

- Pydantic 2.13.4 as the latest stable release with Python 3.14 support;
- pydantic-settings 2.14.2 with Python 3.14 support;
- cattrs 26.1.0 with Python 3.14 support;
- msgspec 0.21.1 with Python 3.14 support;
- platformdirs 4.10.0 as production/stable with Python 3.14 support and a
  22.7 kB universal wheel;
- urllib3 2.7.0 with Python 3.14 support;
- Tenacity 9.1.4 with Python 3.14 support;
- Stamina 26.1.0 with Python 3.14 support;
- HTTPX 0.28.1 as its latest stable release, without a Python 3.14 classifier
  in that stable package metadata.

Those observations must be refreshed at dependency-decision time.

The source-backed conclusions are:

- a package initializer can preserve the `sidekick_usages.cli` import path
  while exposing the application from `cli/app.py`;
- Typer registers `--help` as an option, so the branded `format_help()` logic
  is an adapter rather than a command;
- Python aliases and `TypedDict` describe types but do not validate untrusted
  runtime input;
- standard dataclasses are suitable for trusted runtime models but do not
  validate annotated field types;
- Python packaging guidance does not assign a standard architectural meaning
  to an internal package named `core`; the repository must define and enforce
  that meaning;
- domain dependency guidance consistently places external APIs, filesystem
  mechanisms, configuration-source loading, and operating-system discovery
  outside pure product policy;
- application initialization and concrete dependency construction belong at
  one composition root;
- Pydantic supports runtime validation, serialization, and JSON Schema, while
  `TypeAdapter` can validate dataclasses and typed dictionary shapes without
  requiring core models to inherit `BaseModel`;
- Pydantic boundary validation and `pydantic-settings` source loading are
  separate adoption decisions;
- `platformdirs` provides focused config, data, state, and cache discovery
  across Linux, macOS, and Windows, reducing owned platform logic;
- adding `platformdirs` as a runtime dependency is a direct adoption decision
  even though an older version appears transitively in development tooling;
- dependency adoption does not authorize silent relocation of durable data;
- no global settings model is justified until a cohesive product contract has
  concrete fields, sources, precedence, and consumers;
- HTTP retries require operation-level idempotency decisions, especially for
  POST after ambiguous transport failure;
- `Retry-After` accepts an HTTP date or delay seconds;
- bounded exponential backoff with jitter is the reliability baseline;
- urllib3 combines pooled HTTP transport and HTTP-specific retry policy;
- Tenacity supplies mature transport-independent retry composition but does not
  own HTTP semantics or connection pools;
- the canonical `litl/backoff` repository is archived and is not an adoption
  candidate.

## 21. Approved decisions

The following decisions were approved on 2026-07-09:

1. Use `core/`, not `domain/`, for shared product models, types, and pure
   cross-feature policy.
2. Keep `core/` deliberately narrow and independent of external configuration
   loading, operating-system paths, filesystem I/O, and infrastructure.
3. Use provider-owned Claude and Codex packages.
4. Move concrete heartbeat adapters under provider ownership.
5. Replace flat CLI files with a `cli/` package and nested `commands/`.
6. Keep the help adapter at `cli/help.py`, outside `commands/`.
7. Give Claude and Codex concrete command modules.
8. Adopt the provider command hierarchy with a defined compatibility policy.
9. Keep schemas boundary-local.
10. Complete the validation-library comparison before implementation.
11. Preserve the installed entry point and machine-readable behavior.
12. Use the phased migration and load-bearing test strategy above.
13. Keep shared HTTP infrastructure outside `core/` in a dedicated `http/`
    package.
14. Use exactly one retry owner and require explicit POST operation safety.
15. Make pooling mandatory, use urllib3 `PoolManager` as the preliminary
    transport selection, and compare urllib3 `Retry`, Tenacity, and focused
    local code as the single retry owner in section 10.7.
16. Make `core/expiry.py` the single owner of provider-neutral expiry
    classification.
17. Use aware UTC datetimes internally, acquire wall time through the explicit
    `Clock`, and keep HTTP deadlines on an HTTP-local monotonic source.
18. Keep persistence, provider-native, and human timestamp encoding at their
    owning boundaries; do not create `timestamps.py` or a universal timestamp
    formatter.
19. Use frozen `ApplicationPaths` in top-level `paths.py`, construct it at the
    lazy CLI composition root, and inject concrete locations into their owners.
20. Keep provider-native homes and scheduler installation paths outside
    `ApplicationPaths`.
21. Use `platformdirs` 4.10.0 as the preliminary native-directory adoption
    choice, subject to the explicit dependency, platform, compatibility, and
    migration gate.
22. Preserve existing physical locations during initial path centralization
    and perform any native-location migration as a separate safe change after
    persistence/schema migration is stable.
23. Do not create a global settings model or configuration package without a
    cohesive multi-source product contract; defer `pydantic-settings` until
    that contract exists and passes a separate adoption review.
24. Keep `create_app()` registration-only, compose operational resources
    lazily for executable commands, and expose command-facing services rather
    than paths, clocks, transports, registries, or schedulers in `AppContext`.
25. Preserve generation-aware account-source precedence: canonical and
    existing-Sidekick authoritative stores reconcile explicitly, while the
    prototype store remains an import-only fallback.
26. Make `persistence/migrations.py` the sole durable-state location-migration
    coordinator, keep `paths.py` discovery-only, and consume an injected
    `PrivateAuthMigrator` port so provider auth semantics remain with provider
    adapters without introducing a persistence-to-provider import.

## Sign-off — APPROVED

Approved by the operator on 2026-07-09.

Next, write the matching implementation plan at:

```text
docs/superpowers/plans/
2026-07-09-maintainable-application-architecture.md
```

[python-packages]: https://docs.python.org/3/tutorial/modules.html
[python-src-layout]: https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/
[python-typing]: https://docs.python.org/3.14/library/typing.html
[python-314-annotations]: https://docs.python.org/3.14/reference/compound_stmts.html#annotations
[python-dataclasses]: https://docs.python.org/3.14/library/dataclasses.html
[cockburn-hexagonal]: https://alistair.cockburn.us/hexagonal-architecture
[aws-hexagonal]: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/hexagonal-architecture.html
[microsoft-ddd]: https://learn.microsoft.com/en-us/dotnet/architecture/microservices/microservice-ddd-cqrs-patterns/ddd-oriented-microservice
[cosmic-composition]: https://www.cosmicpython.com/book/chapter_13_dependency_injection
[twelve-factor-config]: https://www.12factor.net/config
[typer-commands]: https://typer.tiangolo.com/tutorial/commands/
[typer-help]: https://typer.tiangolo.com/tutorial/commands/help/
[typer-subcommands]: https://typer.tiangolo.com/tutorial/subcommands/name-and-help/
[typer-reference]: https://typer.tiangolo.com/reference/typer/
[pydantic-models]: https://docs.pydantic.dev/latest/concepts/models/
[pydantic-adapter]: https://docs.pydantic.dev/latest/concepts/type_adapter/
[pydantic-dataclasses]: https://docs.pydantic.dev/latest/concepts/dataclasses/
[pydantic-pypi]: https://pypi.org/project/pydantic/
[pydantic-github]: https://github.com/pydantic/pydantic
[pydantic-settings-docs]: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
[pydantic-settings-pypi]: https://pypi.org/project/pydantic-settings/
[cattrs-docs]: https://catt.rs/en/stable/
[cattrs-pypi]: https://pypi.org/project/cattrs/
[cattrs-github]: https://github.com/python-attrs/cattrs
[msgspec-docs]: https://jcristharif.com/msgspec/
[msgspec-pypi]: https://pypi.org/project/msgspec/
[msgspec-github]: https://github.com/msgspec/msgspec
[rfc-9110]: https://datatracker.ietf.org/doc/html/rfc9110
[rfc-6585]: https://datatracker.ietf.org/doc/html/rfc6585
[aws-retries]: https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html
[aws-jitter]: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
[urllib3-retry]: https://urllib3.readthedocs.io/en/2.7.0/reference/urllib3.util.html
[urllib3-pypi]: https://pypi.org/project/urllib3/
[urllib3-github]: https://github.com/urllib3/urllib3
[tenacity-docs]: https://tenacity.readthedocs.io/en/stable/
[tenacity-api]: https://tenacity.readthedocs.io/en/stable/api.html
[tenacity-pypi]: https://pypi.org/project/tenacity/
[tenacity-github]: https://github.com/jd/tenacity
[httpx-transports]: https://www.python-httpx.org/advanced/transports/
[httpx-pypi]: https://pypi.org/project/httpx/
[stamina-pypi]: https://pypi.org/project/stamina/
[backoff-github]: https://github.com/litl/backoff
[platformdirs-api]: https://platformdirs.readthedocs.io/en/latest/api.html
[platformdirs-platforms]: https://platformdirs.readthedocs.io/en/latest/platforms.html
[platformdirs-pypi]: https://pypi.org/project/platformdirs/
[usage-tui-design]: ./2026-06-19-usage-tui-redesign-design.md
