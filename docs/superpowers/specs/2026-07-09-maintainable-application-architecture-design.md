# Design Spec — Maintainable Application Architecture

- **Status:** **Approved baseline and dependency selections; later persistence
  and migration gates remain**
- **Date:** 2026-07-09
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Scope:** Repository-wide Python application architecture
- **Evidence base:** `develop` at
  `42cd01eb17c7903b385b1b4e259cf5b0c64126c5`
- **Evidence commit subject:** `feat(cli): add shared robot branding`
- **Evidence mode:** Read-only architecture and maintainability analysis
- **Evidence-tree state:** Clean and tracking `origin/develop`
- **Evidence status:** The approved baseline and later decision evidence are
  inlined here; gated decisions are explicitly marked and this repository
  document remains the durable design authority
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
│   ├── expiry.py
│   └── time.py
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
│       ├── migrate.py
│       ├── permissions.py
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
│       ├── auth_migration.py
│       ├── usage.py
│       ├── heartbeat.py
│       └── schemas.py
├── persistence/
│   ├── __init__.py
│   ├── account_store.py
│   ├── artifacts.py
│   ├── assessment.py
│   ├── credential_transaction_schema.py
│   ├── credential_transactions.py
│   ├── errors.py
│   ├── schemas.py
│   ├── migrations/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── account.py
│   │   ├── location.py
│   │   └── ports.py
│   ├── filesystem.py
│   ├── inventory.py
│   ├── locking.py
│   ├── observations.py
│   ├── private_credentials.py
│   ├── transaction.py
│   ├── transforms.py
│   ├── v060.py
│   └── _platform/
│       ├── __init__.py
│       ├── macos.py
│       ├── posix.py
│       └── windows.py
├── credentials/
│   ├── __init__.py
│   ├── models.py
│   ├── service.py
│   └── codex.py
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

The `credentials/` package has three proven responsibilities rather than one
sprawling service module:

- `models.py` owns trusted, provider-neutral credential-workflow inputs and
  results shared by the service and CLI. These are application models, not
  untrusted wire or persistence schemas, so the module is named `models.py`
  rather than `schemas.py`.
- `service.py` owns provider-neutral detection, identity, refresh, save, and
  durable account-update orchestration.
- `codex.py` is the application bridge between provider-owned Codex auth
  preparation and persistence-owned private-bundle transactions. It exists
  because Codex has a real private auth tree and export/login workflow that
  Claude does not have; creating a symmetric `claude.py` without a matching
  responsibility would be speculative.

`credentials/codex.py` may depend on the public Codex auth facade and narrow
persistence transaction types. It does not parse provider payloads, implement
Codex protocol behavior, or become a second persistence writer. The package
initializer remains a thin public facade and does not re-export private
coordinators or provider internals.

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

**Decision:** **GO — OPERATOR APPROVED 2026-07-10**. Adopt platformdirs 4.10.0
privately behind `paths.py` for native discovery using the frozen contract
below. This approval does not authorize physical relocation, which remains
disabled until CS-19 passes its separate migration and rollback gates. The
self-contained evidence record is
[Application Path Discovery Dependency Research][paths-research].

| Option | Benefits | Costs and risks | Decision |
|---|---|---|---|
| Standard library and local per-OS logic | No dependency | Owns XDG, macOS, Windows, WSL, override, and future-platform behavior | NO-GO |
| Keep one hard-coded `Path.home() / ".config"` root | Preserves current behavior with little code | Ignores native macOS and Windows conventions and keeps state/cache lifecycles conflated | Compatibility baseline only |
| `platformdirs` 4.10.0 behind `paths.py` | Focused, maintained, side-effect-free native discovery; pure Python, MIT, no runtime dependencies, Python 3.14 support | Canonical paths differ from the current public contract and require explicit migration | **GO — approved** |
| `pydantic-settings` 2.14.2 | Typed multi-source settings, validation, and precedence | Solves a broader problem that the current application does not have | NO-GO for path discovery |

`platformdirs` remains private to `paths.py`; consumers use `Path` and
`ApplicationPaths`. No provider, service, CLI command, persistence module, or
core module imports it directly.

The frozen discovery call is:

```python
PlatformDirs(
    appname="sidekick-usages",
    appauthor=False,
    version=None,
    roaming=False,
    multipath=False,
    opinion=True,
    ensure_exists=False,
    use_site_for_root=False,
)
```

`appauthor=False` avoids a duplicated Windows component; `roaming=False` keeps
credentials in Local AppData; `version=None` prevents release-path churn;
`opinion=True` preserves the separate Windows cache child;
`ensure_exists=False` keeps discovery read-only; and `use_site_for_root=False`
preserves per-user Unix paths even when the process runs as root.

The intended native semantic mapping is:

- account state and Sidekick-owned private Codex auth bundles are durable,
  credential-bearing application data rooted below `user_data_path`;
- lifetime aggregation totals are safely regenerable data rooted below
  `user_cache_path`; and
- no current artifact uses `user_config_path`, because the application has no
  user settings contract.

The controlled 4.10.0 discovery probe produced:

| Environment | Account store and private Codex root | Lifetime cache |
|---|---|---|
| Linux | `~/.local/share/sidekick-usages/accounts.json`; sibling `codex/` | `~/.cache/sidekick-usages/codex-lifetime-cache.json` |
| Linux with absolute XDG roots | `$XDG_DATA_HOME/sidekick-usages/accounts.json`; sibling `codex/` | `$XDG_CACHE_HOME/sidekick-usages/codex-lifetime-cache.json` |
| WSL | The Linux/XDG contract below the WSL Linux home | The Linux/XDG cache contract |
| macOS | `~/Library/Application Support/sidekick-usages/accounts.json`; sibling `codex/` | `~/Library/Caches/sidekick-usages/codex-lifetime-cache.json` |
| Windows | `%LOCALAPPDATA%\sidekick-usages\accounts.json`; sibling `codex\` | `%LOCALAPPDATA%\sidekick-usages\Cache\codex-lifetime-cache.json` |

The probe also inspected absent controlled roots before and after every path
property and observed no created file or directory. Writers, not discovery,
create only the directories they own.

On Unix, macOS, and WSL, non-empty absolute XDG values are honored. A relative
XDG value is rejected as a typed path-discovery failure before any credential
path is returned. Sidekick does not add a second environment override; tests
inject `ApplicationPaths`. Production Windows composition rejects the
library's `WIN_PD_OVERRIDE_*` testing variables and relies on the operating
system known-folder result. WSL must remain on the Linux filesystem rather than
silently selecting `%LOCALAPPDATA%` or a mounted Windows home.

Package metadata and deterministic class probes are not native relocation
proof. Before activation, real Linux, macOS, Windows, and WSL runners must
confirm the frozen outputs, directory/ACL behavior, collision handling,
rollback, and upgrade-again equivalence. Reverse the dependency and retain
compatibility-path `ApplicationPaths` if those outputs differ, relative-root
safety cannot be enforced, a supported platform is lost, packaging or startup
cost becomes unacceptable, security/maintenance posture degrades, or the
location and latest-state rollback harness cannot pass.

If both authoritative generations exist, the application never silently
chooses one. It either proves they are equivalent under the migration contract
or raises a typed, actionable conflict surfaced by normal commands and
`doctor`. Migration never deletes a compatibility or prototype store
automatically.

#### Location-migration ownership

`paths.py` performs discovery only. `persistence/migrations/service.py` owns
the Sidekick durable-state location-migration workflow and receives
`ApplicationPaths` explicitly. It exposes:

- a read-only assessment describing every candidate, selected generation,
  equivalence or conflict, private-auth work, and recovery action; and
- one idempotent migration operation that coordinates account-state copying,
  provider-owned private-auth validation and copying, persisted-path rewriting,
  permission enforcement, and final atomic account-state commit.

`persistence/migrations/ports.py` defines the narrow typed
`PrivateAuthMigrator` port for assessment, validated copy, permission
enforcement, and destination-collision reporting. The concrete Codex
`auth_migration.py` adapter implements that port. The current composition root
injects the implementation; the persistence package never imports a provider
package directly.

The Codex auth adapter remains the owner of `auth.json` validation and
credential-file semantics. The migration coordinator does not adopt
provider-native home discovery or auth schema logic.

Non-diagnostic commands fail closed when assessment cannot select trustworthy
authoritative state. `doctor` consumes the read-only assessment and remains
able to report source, destination, conflict, partial work, and recovery action
without first requiring a successfully loaded account store. No second path or
migration service is introduced.

For an executable command, runtime composition may assess locations before
loading the store, but it does not silently relocate data. Compatibility paths
remain authoritative until CS-19 explicitly activates the approved migration
protocol. A conflict or partial destination blocks non-diagnostic commands
while still permitting the read-only composition needed by `doctor`. Help and
version bypass assessment entirely.

## 5. Shared core

### 5.1 `core/models.py`

`core/models.py` owns provider-neutral runtime product objects used across
features and adapters.

Initial models:

- `Account`;
- `DetectedCredentials`;
- `ClaudeCredentials`;
- `CodexCredentials`;
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

The CS-13 caller analysis fixes the runtime credential representation as two
immutable, slotted concrete variants:

- `ClaudeCredentials` contains the access token, optional refresh token,
  provider-neutral expiry, and `scopes: tuple[str, ...] | None`;
- `CodexCredentials` contains the access token, optional refresh token,
  provider-neutral expiry, optional provider account identity, optional auth
  home, optional ID token, and the bounded opaque Codex auth
  `last_refresh` value; and
- `type Credentials = ClaudeCredentials | CodexCredentials` is the complete
  supported union. No abstract credential base, generic provider hierarchy,
  or speculative capability hook is introduced.

Each credential variant exposes its `ProviderId` as class-level closed
vocabulary. `Account.provider_id` is derived from the credential variant; it
is not a second mutable discriminator that can disagree with the stored
credentials. Claude credentials therefore cannot contain Codex identity or
auth-file fields, and Codex credentials cannot contain Claude scopes.

`Account` remains a mutable, slotted runtime object because rename, plan,
refresh-audit, and heartbeat workflows update account state. Its
`credentials` member is replaced atomically only after a complete provider
response has been parsed and validated. Tokens are never updated one field at
a time before expiry or identity validation finishes.

`DetectedCredentials` contains the same immutable `Credentials` union plus
the detected plan. It may expose provider-neutral read-only properties for
access token, refresh token, expiry, and provider identity, but it does not
duplicate every provider field. CLI-only discovery context such as the source
Codex home remains an application workflow argument rather than becoming core
credential state.

The credential token, refresh token, and Codex ID token fields use
`repr=False`. The containing `Account` and `DetectedCredentials`
representations must therefore remain secret-free without custom redaction at
each call site.

`CodexCredentials.auth_home` remains a string in core so core does not import
`pathlib`; the Codex filesystem adapter converts it to `Path`. The Codex auth
`last_refresh` value is the one deliberate provider-native time exception: it
remains a bounded opaque string because only the Codex auth-file boundary owns
its semantics and lossless encoding. It is not classified, compared, or
formatted by core.

Persistence flattens the runtime credential variant into the approved stored
record and reconstructs the variant from the persisted provider
discriminator. The flattened schema is a boundary representation, not a
reason to permit provider-incompatible fields in the runtime model.

`UsageWindow` and `UsageReport` are immutable, slotted runtime values.
`UsageWindow.resets_at` is an aware `datetime | None`; `UsageReport.windows`
uses an immutable tuple. `UsageReport.raw` is deleted. Provider schemas own
the original payload, and production consumers receive only validated product
data.

`core/` owns provider-neutral product vocabulary and pure cross-feature policy.
It does not discover configuration, load external settings sources, resolve
operating-system paths, perform filesystem or network I/O, or depend on
infrastructure frameworks. Operational configuration remains with the feature
or adapter whose behavior it governs and is composed at the application entry
point.

Persistence is not an `Account` method.

### 5.2 `core/types.py`

`core/types.py` owns shared type vocabulary, not every annotated class.

Shared types:

- `ProviderId` as a closed `StrEnum`;
- `AccountLabel` as a validating, non-normalizing `str` value object;
- `RefreshStatus`, `HeartbeatStatus`, and `ExpiryState` as closed
  cross-feature `StrEnum` vocabularies; and
- `ExitCode` as the existing shared closed exit vocabulary.

`AccountLabel` subclasses `str` and validates in `__new__`. A valid label is
non-empty, contains no Unicode control or NUL character, is valid UTF-8, and
contains no more than 512 encoded UTF-8 bytes. Construction preserves the
exact Unicode sequence. It never trims, normalizes, case-folds, or otherwise
changes the label.

A PEP 695 alias and `NewType` were rejected because neither enforces the
confirmed persistence and command invariants. A wrapper dataclass was rejected
because labels are real JSON object keys and are passed directly to Rich,
Typer, shell quoting, dictionaries, and filesystem-boundary helpers. A
validated `str` value object enforces the invariant without forcing conversion
at every current string consumer. CLI and persistence boundaries translate
construction failures into their own typed error vocabulary.

The cross-module status enums contain exactly the current closed values:

- `RefreshStatus`: `ok`, `skipped`, and `failed`;
- `HeartbeatStatus`: `warmed`, `active`, `disabled`, `unsupported`, `failed`,
  and `enabled`; and
- `ExpiryState`: `valid`, `expired`, `unknown`, and `invalid`.

The existing duplicated string constants are removed when callers migrate;
they do not remain as a second authority. Plan names, provider response
phrases, presentation-only labels, and maintenance's near-expiry policy remain
open or feature-local and do not gain enums.

Feature-specific statuses stay with their feature models. Provider wire
shapes and persisted records never belong in `core/types.py`.

### 5.3 `core/expiry.py`

Provider adapters normalize expiry into an aware UTC `datetime` before
constructing core models. Stored runtime expiry is a discriminated union:

- `KnownExpiry(at: datetime)` stores one authoritative instant;
- `UnknownExpiry` stores no instant; and
- `InvalidExpiry` stores no authoritative instant and forces explicit failure
  handling.

`type Expiry = KnownExpiry | UnknownExpiry | InvalidExpiry` is the account and
credential field type. Timed expiry classes reject naive datetimes and
canonicalize aware inputs to `datetime.UTC`.

Classification is a separate pure result because validity changes as the
clock advances:

- `ValidExpiry(at: datetime)`;
- `ExpiredExpiry(at: datetime)`;
- `UnknownExpiry`; and
- `InvalidExpiry`.

`type ClassifiedExpiry` is the union of those four results. The valid and
expired classes always carry an aware UTC instant; unknown and invalid classes
cannot carry one. A mutable `state` plus optional timestamp object is
prohibited because it permits contradictory states.

It is consumed by usage checking, maintenance, doctor, and heartbeat
readiness. Unknown providers and invalid provider-native units fail at the
provider or schema boundary; they never select a default normalization path.
Core expiry policy receives one explicit aware `now` value. A known expiry is
valid only when `at > now`; equality and every earlier instant are expired.
The classifier does not import the system clock, read configuration, parse
provider units, or format timestamps.

An absent or null provider expiry becomes `UnknownExpiry`. A present value
with the wrong type, a Boolean accepted as an integer, an out-of-range epoch,
or another malformed representation becomes `InvalidExpiry` or the owning
typed boundary error; it never becomes epoch zero, unknown, or a plausible
default. Ordinary persistence and provider-refresh paths reject invalid input
before producing a usable saved account. Services handed `InvalidExpiry`
still fail closed: usage does not issue a provider request, heartbeat does not
warm, maintenance does not report success or fresh state, and doctor reports
the explicit invalid condition.

Maintenance alone applies provider-specific refresh margins to a
`ValidExpiry.at`. The Codex usage check's proactive refresh margin also remains
use-case policy. Neither margin becomes a stored expiry state or changes the
core boundary rule.

### 5.4 `core/time.py`

`core/time.py` owns exactly one shared runtime invariant:
`as_utc(value: datetime) -> datetime`. It rejects naive values and returns the
equivalent aware `datetime.UTC` instant. The helper was extracted only after
the same invariant had concrete consumers in expiry values, mutable account
runtime/reset state, usage windows, and heartbeat results.

The module does not acquire a clock, parse provider epochs or persisted
strings, format a timestamp, sleep, choose expiry policy, or encode provider,
persistence, or display representations. Those operations remain at their
owning boundaries. This narrow pure datetime normalizer is therefore not the
prohibited `timestamps.py` or a universal timestamp-string formatter.

### 5.5 Feature-local models

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

### 5.6 CS-13 migration constraints

The atomic model migration preserves these caller-observed distinctions and
corrects these proven hazards:

- Claude `scopes=None` means unknown scopes and alone selects the default
  refresh scopes. `scopes=()` means known-empty and remains empty during
  refresh. Truthiness must not collapse them.
- A missing detected field usually means no new information; known-empty
  scopes deliberately clear stale scope metadata.
- A successful Claude or Codex refresh requires a non-empty access-token
  string. A present replacement refresh token, and a present Codex ID token,
  must also be non-empty strings; malformed optional fields reject the whole
  refresh before credential replacement.
- A Claude refresh response without new expiry data produces
  `UnknownExpiry`; it never preserves a stale known expiry. A present but
  undecodable Codex access-token JWT produces `InvalidExpiry`, while a decoded
  JWT with no `exp` claim remains `UnknownExpiry`.
- Expiry is inspected by discriminant, never by integer or object truthiness.
- Credential refresh parses and validates the full response before replacing
  the immutable credential variant.
- Token lookup is provider-scoped so equal token strings cannot merge Claude
  and Codex identities.
- Account change detection compares typed credential and plan values; it does
  not serialize the domain object merely to detect mutation.
- Token masking moves to the account-list renderer; it is presentation
  behavior rather than an `Account` method.
- Missing-account outcomes use absent typed identities rather than fabricated
  label `?` or provider `unknown`; the renderer owns any visual fallback.
- `heartbeat_5h_reset_at` and `heartbeat_window_resets` both remain in the
  runtime migration because the approved persisted contract accepts them
  independently. The reset mapping is defensively copied, normalized, and
  exposed read-only so later item mutation cannot bypass the aware-UTC
  invariant. Legitimate updates replace the mapping copy-on-write. Divergent
  historical records must not be collapsed.
- Heartbeat provider and window result `reset_at` values enforce the same
  aware-UTC invariant at construction rather than relying on a later account
  write to normalize them.
- The current Codex usage-check margin and the separate Claude and Codex
  maintenance margins remain with their use cases.

### 5.7 CS-13 load-bearing proof

CS-13 adds or consolidates only these behavior tests:

1. One parameterized `AccountLabel` test proves exact Unicode preservation and
   rejects empty, control or NUL, unencodable, and 513-byte labels.
2. One table-driven expiry test covers immediately before, exactly at, and
   immediately after one explicit `now`, plus unknown and invalid states. One
   focused assertion proves timed states reject naive datetimes and untimed
   states cannot carry an authoritative instant. The same invariant test
   proves mutable audit fields normalize every assignment and the nested
   heartbeat-reset map cannot be mutated in place.
3. One two-provider model test proves provider identity is derived from the
   credential variant and synthetic access, refresh, and ID tokens are absent
   from `Account` and `DetectedCredentials` representations.
4. One parameterized provider-boundary test proves Claude milliseconds and
   Codex seconds for the same instant produce the same `KnownExpiry.at` and
   malformed native values never become epoch or unknown.
5. One synthetic two-account compatibility-store round trip proves legacy
   Claude and Codex units plus audit and reset strings become aware runtime
   values and encode exactly, including precision rejection.
6. One existing service-clock test proves a decision samples wall time once.
   The existing CS-11 monotonic-deadline suite is rerun, not duplicated.
7. One parameterized refresh-boundary test proves known-empty Claude scopes,
   absent Claude expiry, non-empty token fields, and malformed Codex JWT
   distinctions; one heartbeat-boundary test proves aware offset values become
   UTC and naive resets fail.
8. One architecture and built-wheel test proves `core/models.py`,
   `core/types.py`, `core/expiry.py`, and `core/time.py` obey the import
   boundary and neither source nor wheel contains stale `report.py`.

Existing behavior tests are mechanically adapted only where their boundary
remains distinct. Redundant one-field account round trips, repeated scope
fixtures, `raw={}` setup, and duplicate clock tests are removed instead of
being preserved as coverage padding.

### 5.8 Lifetime collection outcomes

`lifetime.py` owns one feature-local closed result:

- `LifetimeTotal(output_tokens: int, since: date | None)` represents a valid
  non-negative total, including zero;
- `LifetimeUnavailable` means the provider has no local source corpus; and
- `LifetimeFailure(kind)` distinguishes source-unreadable,
  source-malformed, cache-read, and cache-write failures.

Claude's statistics file and Codex's rollout tree remain provider-native
source locations owned by the lifetime feature. Only the Sidekick Codex cache
path comes from injected `ApplicationPaths`. Dates are parsed at the source
boundary, and rendering receives completed typed results without filesystem
access.

Codex source enumeration propagates traversal errors, uses canonical relative
paths below the sessions root as collision-safe cache keys, and compares exact
integer nanosecond modification times. The exact released basename-plus-float
cache shape is a known legacy generation: its totals are never trusted, every
rollout is reread, and the current cache is published only after the complete
source pass succeeds. Arbitrary or unreadable cache state remains an explicit
failure. A failed rollout never lowers a total or writes a partial cache.

The usage command collects lifetime state once for each provider represented
by the selected accounts, including when every usage request fails. Both wide
and narrow renderers preserve totals, unavailability, and failures. A
lifetime failure renders before exit and contributes `SYSTEM_ERROR`; absence
of local lifetime data is not itself an error.

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

CS-13 fixes the ownership and runtime representation for every existing time
family:

| Value | Runtime representation | Decode owner | Encode owner |
|---|---|---|---|
| Claude token expiry milliseconds | `KnownExpiry` in aware UTC | Claude adapter | Current compatibility store until CS-14 |
| Codex JWT expiry seconds | `KnownExpiry` in aware UTC | Codex adapter | Current compatibility store until CS-14 |
| Provider `expires_in` | `KnownExpiry` from one clock sample | Owning provider adapter | Current compatibility store |
| Usage-window reset | Optional aware `datetime` | Owning provider schema | Usage renderer |
| Heartbeat reset | Optional aware `datetime` | Heartbeat provider adapter | Heartbeat renderer and store |
| Refresh and heartbeat audit time | Optional aware `datetime` | Current compatibility store | Current compatibility store and renderers |
| Codex auth `last_refresh` | Optional bounded opaque `str` | Codex auth adapter | Codex auth adapter |
| HTTP retry deadline | Monotonic duration only | HTTP retry owner | Never encoded as wall time |

Provider-native integer decoding uses epoch arithmetic with `timedelta`, not a
floating-point timestamp round trip. Compatibility encoding is exact:

- a Claude expiry must be representable as an exact millisecond;
- a Codex expiry must be representable as an exact second; and
- a non-representable runtime value is a typed boundary failure rather than a
  truncated integer.

When a relative refresh lifetime is converted to an absolute expiry, the
owning provider quantizes the result to the native precision it can later
encode exactly. It does not let a higher-precision clock sample enter core and
silently discard that precision during persistence.

The current store owns legacy millisecond, second, and audit-string conversion
until CS-14 replaces it with the versioned persistence schema. Provider
adapters own wire conversion. Renderers receive aware datetimes and an
explicit reference time and perform only human formatting. Doctor's JSON
builder owns its canonical machine timestamp strings. Core imports none of
those encoders.

### 6.4 Completed build-versus-adopt research

**Decision:** **GO — OPERATOR APPROVED 2026-07-10**. Use Pydantic 2.13.4
`TypeAdapter` at untrusted provider and persistence boundaries. The approval
authorizes implementation of the dependency and its required packaging work;
release exposure remains blocked until the Homebrew source-build and supported
platform gates pass. The self-contained evidence record is
[Schema Validation Dependency Research][schema-research].

The synthetic corpus covered eight current, historical, prototype, provider,
JWT, refresh, and generic-JSON boundary families. Eight independent mutations
covered missing and extra fields, nulls, wrong scalar and container types,
Boolean coercion, nested list errors, wrong roots, and simultaneous account
errors. All four strict prototypes rejected all eight mutations. Their
maintainability and diagnostic behavior differed:

| Option | Measured result | Research disposition |
|---|---|---|
| Standard library | Strictness was possible only by owning recursion, union handling, aggregation, paths, and messages across eight families | NO-GO; this becomes a custom schema framework |
| Pydantic 2.13.4 `TypeAdapter` | Aggregated independent errors with exact account labels, nested indices, and stable codes | **GO — approved** |
| cattrs 26.1.0 | Aggregated useful paths while keeping dataclasses independent, but required owned strict scalar hooks and had weaker union diagnostics | Leading fallback if the packaging gate rejects Pydantic |
| msgspec 0.21.1 | Fastest and smallest measured candidate, but stopped at the first error and omitted the account label from a mapping path | NO-GO for operator repair diagnostics |

Decision-critical Linux CPython 3.14.6 measurements were:

| Measure | Standard library | Pydantic | cattrs | msgspec |
|---|---:|---:|---:|---:|
| Decode and validate median | 7.820 us | 4.449 us | 13.194 us | 1.145 us |
| Fresh schema process median | 44.81 ms | 103.33 ms | 72.06 ms | 48.01 ms |
| Installed closure | 0 | 7,281,761 bytes | 677,508 bytes | 532,136 bytes |

The Pydantic closure contains five distributions and its measured Linux wheel
downloads total 2,645,819 bytes. Runtime throughput is not the selection
reason; structured multi-error diagnostics are. Actual help, dashboard, and
doctor startup still require measurement on the implemented composition path.

Pydantic's release gate is material. The current Homebrew generator selects
source distributions. `pydantic-core` uses Rust and maturin, requires Rust
1.88 for the measured release, and the official Homebrew Pydantic formula
declares both build tools. Mainstream wheels do not prove Sidekick's formula
path. Production adoption therefore requires a deterministic, supportable,
network-restricted Homebrew source build plus the Linux, macOS, and Windows
runtime matrix. If that cost is rejected or cannot pass, reopen CS-07 with
cattrs as the leading alternative; do not switch silently.

If Pydantic is approved, the following rules are mandatory:

- use cached, module-local adapters only in provider and persistence boundary
  modules;
- configure strict behavior explicitly and choose the extra-field policy per
  boundary; stored generations are closed, while provider compatibility must
  be intentional rather than globally forbidden;
- validate boundary `TypedDict` or dataclass shapes and convert immediately to
  plain application models;
- project `ValidationError.errors(include_input=False, include_url=False)` to
  Sidekick-owned path, code, and safe-message values;
- never expose, chain, log, render, or serialize raw Pydantic errors or rejected
  inputs; and
- keep Pydantic out of `core/`, commands, services, renderers, public models,
  and public method signatures.

Reopen or reverse the recommendation if the Homebrew build cannot be made
deterministic, raw input escapes diagnostics, framework types leak into core or
public surfaces, an upgrade changes strict behavior without a migration path,
or security, provenance, maintenance, or supported-platform posture becomes
unacceptable.

This decision does not approve `pydantic-settings`. Boundary validation and
configuration-source discovery are separate problems; the latter remains
deferred until section 3.10 has a real multi-source settings contract.

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
- installs the lazy typed composer set from `cli/context.py` in `ctx.obj`;
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
    register_migration_commands(app)
    register_permission_commands(app)
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
composition path discovers paths, constructs the clock, opens the pooled HTTP
client, constructs adapters and services, and returns one typed context.
`cli/context.py` owns the generic resource-transfer mechanism and lazy
`ctx.obj` accessors plus the concrete production factories; `cli/app.py`
installs those callables without invoking them. No command module constructs a
provider, store, HTTP client, scheduler, clock, or credential coordinator.

There are five explicit composers because the command families have different
safety and initialization requirements:

- `compose_app_context()` builds the normal store-backed services;
- `compose_persistence_context()` builds explicit migration, rollback, and
  permission-repair operations without requiring a runtime store;
- `compose_doctor_context()` can return a ready, blocked, or failed diagnostic
  state without forcing store construction;
- `compose_daemon_context()` builds only the scheduler-management service; and
- `compose_update_context()` builds only the update service and the resources
  that service owns.

The names describe real command boundaries, not a generic service locator.
Each composer returns `Composed[T]`, where `T` is exactly one of the five
context types. A small PEP 695 owner transfers an `ExitStack` only after every
constructor succeeds:

```python
@dataclass(slots=True)
class Composed[T]:
    value: T
    _resources: ExitStack = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Close owned resources at most once."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()
```

Composition builds resources in a local `ExitStack`, calls `pop_all()` only
after the typed value is complete, and returns the transferred stack inside
`Composed[T]`. A partial construction unwinds every acquired resource in LIFO
order. If construction fails and cleanup succeeds, the exact construction
exception is re-raised with a bare `raise`. If cleanup also fails, both
failures are surfaced in an exception group with the construction failure
first; cleanup must never replace or silently discard the original failure.

The root callback installs one lightweight `InvocationContext` in `ctx.obj`.
It stores the typed composer callables and, only after a command asks for one,
the resulting `Composed[T]`. Its `require_app()`, `require_persistence()`,
`require_doctor()`, `require_daemon()`, and `require_update()` methods:

1. return the already composed value when called again;
2. otherwise invoke only the matching composer;
3. register that owner's idempotent `close()` exactly once with the root Click
   context; and
4. return the typed value, never an optional or union of unrelated contexts.

Commands accept `typer.Context`, read `ctx.obj`, and call the one appropriate
`require_*()` accessor. There is no module singleton, mutable class-level
context, `set_context()`, implicit `get_current_context()` dependency, or
truth-value fallback. Tests inject composer functions or a fully constructed
`InvocationContext` through `CliRunner.invoke(..., obj=...)`.

Production provider and heartbeat registries use explicit absence semantics.
Only `None` requests construction of the production registry. An injected
empty mapping is copied and remains empty through every service and doctor
composition path. Code such as `providers or build_provider_registry()` is
forbidden.

Help and version paths do not call runtime composition. They do not discover
application paths, load an account store, inspect credentials, create
directories, construct a scheduler backend, or initialize HTTP pools.

### 8.3 Application context

`cli/context.py` owns frozen, command-facing contexts. `AppContext` is strict:
every field is a precomposed application or persistence service, or a narrow
provider capability with a concrete command consumer. It contains no lazy
optional state and no composition inputs.

```python
@dataclass(frozen=True, slots=True)
class AppContext:
    accounts: AccountStore
    usage: UsageCheckService
    credentials: CredentialService
    heartbeat: HeartbeatService
    maintenance: TokenMaintenanceService
    lifetime: LifetimeCollector
    claude_setup_token: ClaudeSetupToken


@dataclass(frozen=True, slots=True)
class PersistenceContext:
    persistence: PersistenceCommands


@dataclass(frozen=True, slots=True)
class DoctorReady:
    service: DoctorService
    assessment: PersistenceAssessment


@dataclass(frozen=True, slots=True)
class DoctorBlocked:
    assessment: PersistenceAssessment


@dataclass(frozen=True, slots=True)
class DoctorFailed:
    failure: PersistenceCompositionFailure


type DoctorState = DoctorReady | DoctorBlocked | DoctorFailed


@dataclass(frozen=True, slots=True)
class DoctorContext:
    state: DoctorState


@dataclass(frozen=True, slots=True)
class DaemonContext:
    daemon: DaemonManager


@dataclass(frozen=True, slots=True)
class UpdateContext:
    update: UpdateService
```

`AccountStore` is the one proven direct persistence service for simple account
CRUD and read-only account display. It is not exposed to update, daemon,
persistence-recovery, or blocked-doctor commands. Provider-specific commands
use `CredentialService` or the narrow `ClaudeSetupToken` capability; no
command-facing context receives an entire provider registry for convenience.

`DoctorState` is an explicit sum type rather than three optional fields.
`DoctorReady` alone contains a `DoctorService` and validated persistence
assessment. `DoctorBlocked` contains the safe migration or corruption
assessment that the command can render without a store. `DoctorFailed`
contains the bounded, secret-safe composition failure. The doctor command uses
`isinstance()` plus `assert_never()` so every state has one visible outcome.
Blocked and failed states must not attempt `AccountStore` construction.

Console and error-console presentation ports belong to the lightweight
invocation adapter, not any operational context. This keeps `AppContext`
service-only and every narrow recovery context free of raw infrastructure
while preserving injected output capture in command tests. Command options,
the root provider filter, and other invocation-local values remain on the
Click invocation or ordinary local variables.

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

Typer and Click carry `InvocationContext` through `ctx.obj`. A missing object,
wrong object type, or composer returning the wrong context fails explicitly;
an accessor never invents production defaults inside a test invocation.

`cli/context.py` is private application composition infrastructure. It is not
exported as a general service API.

An explicitly empty provider or heartbeat registry remains empty. Only `None`,
where permitted by a production composer, selects a default.

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

Provider-neutral commands remain grouped by workflow. Ownership includes the
command's validation and presentation helpers so workflows are not split back
across a generic utility module:

| Module | Exact ownership |
|---|---|
| `usage.py` | default invocation, `check`, no-account result, lifetime collection call, and usage exit reduction |
| `accounts.py` | `list`, `remove`, `rename`, `set-plan`, `reset`, account-table rows, and account mutation messages |
| `credentials.py` | `add`, the sole `refresh` registration/parser, refresh-mode validation, missing-only token prompt fallback, credential-failure output, and refresh outcome output |
| `heartbeat.py` | heartbeat group, label fallback, enable, disable, status, heartbeat outcome output, and heartbeat exit reduction |
| `maintenance.py` | `maintain`, quiet/scheduled filtering, combined maintenance output, and combined exit reduction |
| `doctor.py` | `doctor`, exhaustive `DoctorState` handling, filtering, diagnostic output, and doctor exit reduction |
| `migrate.py` | account migration, rollback preparation, preview, confirmation, success output, and persistence error mapping |
| `permissions.py` | permission-repair preview, confirmation, execution, and result output |
| `daemon.py` | daemon group, install, status, uninstall, backend validation, result output, and scheduler exit mapping |
| `updates.py` | `check-update`, `update`, update result output, and update exit mapping |
| `claude.py` | current setup-token adapter and typed setup-token outcome output |
| `codex.py` | current `codex-login` and `codex-export` adapters and their result output |

`cli/app.py` alone owns root application construction, the root callback,
global option parsing, the version callback, and explicit registration calls.
`cli/context.py` alone owns typed context models, resource ownership, lazy
require accessors, and production composition functions. `cli/token_input.py`
owns only hidden/piped token acquisition and token-pattern matching.
`cli/help.py` owns only the help adapter described above.

`cli/commands/__init__.py` is intentionally empty or exports only documented
registration functions. It is not a barrel for command helpers. A helper used
by one workflow stays in that workflow's module. A helper moves only after
three concrete owners prove one shared semantic contract.

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

The live 0.6.0 tag, project version, and Release Please manifest establish the
first provider-hierarchy release as `R = 0.7.0`. Deprecated aliases ship in
0.7.0, remain available throughout 0.8.x, and are removed in 0.9.0. The
changelog and help mark them deprecated in 0.7.0; deprecation messaging never
contaminates JSON, quiet, scheduled, or other machine-readable stdout.

## 9. Application services

### 9.1 Usage checking

`usage/service.py` owns selection, usage collection, refresh-and-retry policy,
and failure aggregation.

```python
@dataclass(frozen=True, slots=True)
class UsageCheckResult:
    usages: tuple[AccountUsage, ...]
    failures: tuple[FetchFailure, ...]
    reference_time: datetime


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
- persists successful credential changes explicitly; and
- returns the single aware UTC reference time used for the check so the CLI
  renderer does not need a raw `Clock` in `AppContext`.

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

Credential persistence uses one crash-recoverable, provider-neutral
transaction under the account lock. A bounded deterministic tuple of private
bundle writes is journaled together, duplicate destinations are rejected,
every target bundle is validated, and account authority is committed last.
The journal distinguishes source guards from target authority expectations so
the same boundary supports in-place refresh and compatibility-to-canonical
relocation. Fresh-process recovery rolls the entire tuple back when authority
is at the base generation, rolls it forward when authority is at the target
generation, and fails closed on any third authority. Provider refresh remains
pure and cannot write a private bundle before the application commits.

Credential states distinguish:

- missing;
- unreadable;
- malformed;
- incomplete;
- expired;
- rejected;
- identity mismatch;
- unsupported.

`credentials/models.py` also owns `TokenPromptSpec`, the immutable,
non-secret metadata needed by `cli/token_input.py`: provider id, display name,
compiled token pattern, and an optional provider-specific setup hint.
`CredentialService.prompt_spec()` returns that metadata without exposing a
provider adapter or registry. The CLI prompts only after a local detection
result is exactly the typed missing state. Unreadable, malformed, incomplete,
expired, rejected, identity-mismatched, and unsupported results are rendered
directly and never trigger a token prompt.

### 9.3 Narrow supporting services and capabilities

The CLI package conversion closes the remaining raw-dependency leaks with the
smallest caller-proven seams:

- `LifetimeCollector` in top-level `lifetime.py` owns the provider-to-source
  mapping and exposes `collect(provider_ids)`. It returns typed
  `LifetimeResult` values and never prints. `AppContext` does not expose raw
  callables or cache paths.
- `HeartbeatService.support_label(account)` and
  `support_labels(accounts)` provide the account-list, doctor, and
  heartbeat-status display data. The collection method returns a typed mapping
  keyed by `AccountLabel`. Commands and renderers do not receive the heartbeat
  adapter registry. Provider display logic remains behind the heartbeat
  service rather than moving into CLI helpers.
- `UpdateService` in top-level `update.py` owns release checking, install
  method detection, upgrade-command selection, and injected command
  execution. `UpdateContext` exposes only this service; the update commands do
  not receive `HttpClient` or call `subprocess` directly.
- `ClaudeSetupToken` is a narrow protocol implemented by the Claude facade.
  It exposes only the typed setup-token capture operation. The generic
  provider contract and `AppContext` never expose a concrete
  `ClaudeProvider` merely for this one capability.
- `TokenPromptSpec` carries only the safe provider metadata needed for token
  entry. It is obtained through `CredentialService`, not a raw provider map.

These seams have concrete callers in the current command surface and remove
raw infrastructure from contexts. They do not add hooks, optional strategies,
or generic extension systems.

### 9.4 Expiry use in application services

`core/expiry.py` is the single owner of provider-neutral expiry
classification. Each application service obtains one aware UTC `now` value
from its injected `Clock` and passes the value into core policy. Services do
not read provider-native epoch units or compare formatted timestamp strings.

Maintenance alone derives "due for refresh" from a valid expiry and its
provider-specific refresh margin. The margin is maintenance use-case policy;
it is neither a persisted expiry state nor a core discriminant. Usage checking,
doctor, and heartbeat consume the core classification without inheriting
maintenance-specific thresholds.

### 9.5 Account persistence

`persistence/account_store.py` owns:

- validated loading;
- known-schema migrations;
- account querying;
- durable account persistence;
- atomic writes where supported;
- explicit persistence errors.

The `persistence/migrations/` package owns both explicitly supported
stored-schema migrations and the durable-state location-migration coordinator
defined in section 4.2. Its thin initializer preserves the public migration
facade; `service.py` is the sole writer, `account.py` owns current generation
transitions, `location.py` owns pure assessment/planning, and `ports.py` owns
the injected interfaces. It does not discover paths, parse provider-native
homes, or own Codex auth schemas, and it never imports a provider package.

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

#### Approved stored-schema and recovery contract

**Decision:** **GO — OPERATOR APPROVED 2026-07-10**. Use the explicit,
versioned, crash-recoverable protocol in this section. Adopt Portalocker 3.2.0
for the participating-process hard lock and pywin32 312 as a direct
Windows-only dependency for native file and DACL operations. Do not use a
generic atomic-write dependency. The self-contained evidence record is
[Persistence Durability and Rollback Research][persistence-research].
The exact implementation closure is
[Persistence Contract Closure Research](../research/2026-07-10-persistence-contract-closure.md),
and the normative classifier is
[Persistence Assessment State Machine](../research/2026-07-10-persistence-assessment-state-machine.md).
CS-14 implements all three tracked authorities together; broad wording in an
earlier section never weakens an exact bound, transition, or native gate in
these authorities.

This decision authorizes the contract and later implementation. It does not
enable the versioned writer before the native platform, built-artifact, and
actual-v0.6.0 recovery gates pass.

##### Supported stored inputs

The prototype import input is a uniform top-level label map whose records have
exactly two required, non-empty strict strings:

```json
{
  "claude-max-1": {
    "token": "test-only-claude-access-token",
    "plan": "max"
  }
}
```

It is accepted only from `AccountLocations.prototype_cc_usage`, only when no
authoritative Sidekick account file exists, and only through the explicit
migration command. Mixed prototype/current records, extra fields, wrong types,
and empty tokens fail. The prototype file is never modified or deleted.

The unversioned Sidekick map is generation zero. One closed historical schema
supports every released writer without an ambiguous union:

| Release family | Required or permitted fields | Missing-field default |
|---|---|---|
| v0.1.0 | Required `provider_id`, `access_token`, `refresh_token`, `expires_at`, `plan` | None; all five keys must exist |
| v0.2.0-v0.3.0 | Adds `scopes` | `null` |
| v0.4.0-v0.4.1 | Adds `provider_account_id`, `codex_home`, `codex_id_token`, `codex_last_refresh`, `last_refresh_at`, `last_refresh_status`, `last_refresh_error` | `null` |
| v0.5.0-v0.6.0 | Adds `heartbeat_enabled`, `heartbeat_5h_reset_at`, `heartbeat_window_resets`, `heartbeat_targets`, `last_heartbeat_at`, `last_heartbeat_status`, `last_heartbeat_error` | `false` for enabled; otherwise `null` |

All present generation-zero scalars and containers are strict. A string such
as `"false"` is not Boolean true, Boolean is not an integer, and a mixed list
or mapping never filters invalid elements. Unknown fields fail.
`provider_id` is exactly `claude` or `codex`.

The current complete synthetic generation-zero corpus is:

```json
{
  "claude-max-1": {
    "provider_id": "claude",
    "provider_account_id": null,
    "access_token": "test-only-claude-access-token",
    "refresh_token": "test-only-claude-refresh-token",
    "expires_at": 1783771200000,
    "plan": "max",
    "scopes": ["user:profile"],
    "codex_home": null,
    "codex_id_token": null,
    "codex_last_refresh": null,
    "last_refresh_at": "2026-07-10T12:00:00Z",
    "last_refresh_status": "ok",
    "last_refresh_error": null,
    "heartbeat_enabled": false,
    "heartbeat_5h_reset_at": null,
    "heartbeat_window_resets": null,
    "heartbeat_targets": null,
    "last_heartbeat_at": null,
    "last_heartbeat_status": null,
    "last_heartbeat_error": null
  },
  "codex-plus-1": {
    "provider_id": "codex",
    "provider_account_id": "acct_test_only",
    "access_token": "test-only-codex-access-token",
    "refresh_token": "test-only-codex-refresh-token",
    "expires_at": 1783771200,
    "plan": "plus",
    "scopes": null,
    "codex_home": "/synthetic/sidekick/codex/codex-plus-1",
    "codex_id_token": "test-only-codex-id-token",
    "codex_last_refresh": "2026-07-10T12:00:00Z",
    "last_refresh_at": null,
    "last_refresh_status": null,
    "last_refresh_error": null,
    "heartbeat_enabled": true,
    "heartbeat_5h_reset_at": "2026-07-10T17:00:00Z",
    "heartbeat_window_resets": {
      "standard": "2026-07-10T17:00:00Z"
    },
    "heartbeat_targets": ["standard"],
    "last_heartbeat_at": "2026-07-10T12:00:00Z",
    "last_heartbeat_status": "active",
    "last_heartbeat_error": null
  }
}
```

##### Version-one schema

The first versioned document uses strict integer version `1` and exactly two
top-level fields in this order:

```json
{
  "schema_version": 1,
  "accounts": {}
}
```

`accounts` preserves insertion order. Every version-one record emits the same
20 fields shown in the complete generation-zero corpus and in that order.
`provider_id` is the Pydantic discriminator:

- Claude requires `provider_account_id`, `codex_home`, `codex_id_token`, and
  `codex_last_refresh` to be null;
- Codex requires `scopes` to be null;
- shared optional values remain explicit nulls; and
- provider-incompatible non-null combinations fail.

The version-one output for the corpus above is the exact two-field envelope
whose `accounts` value contains those two records, except:

- Claude `expires_at` is `"2026-07-11T12:00:00.000000Z"`;
- Codex `expires_at` is `"2026-07-11T12:00:00.000000Z"`; and
- every non-null Sidekick-owned refresh, heartbeat, and reset timestamp is
  canonicalized to six fractional digits and UTC `Z`.

All Sidekick-owned version-one times use exactly:

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

Claude expiry precision must be an exact millisecond; Codex expiry precision
must be an exact second. That makes the reverse provider-native integer
conversion exact. `codex_last_refresh` remains a bounded provider-native
string because the Codex auth-file boundary owns its meaning.

Refresh status is exactly null, `ok`, `skipped`, or `failed`. Persisted
heartbeat status is exactly null, `warmed`, `active`, `disabled`,
`unsupported`, `failed`, or `enabled`. Adding a field or persisted state
requires schema version two and an amendment to this recovery contract; it is
never added to version one as a permissive optional extra.

Every valid version-one state except explicit empty heartbeat target or reset
collections is losslessly representable by the actual v0.6.0 store. The pinned
reader's `_str_list()` and `_str_dict()` return `result or None`, making `[]`
and `{}` indistinguishable from null after load. Version one preserves the
richer distinction. Rollback preparation detects those two valid states and
raises `RollbackCompatibilityError` before any snapshot or authority mutation;
it never coerces them while claiming a lossless downgrade.

##### JSON lexical and deterministic-output rules

The persistence boundary enforces:

- UTF-8 without a byte-order mark;
- a 16 MiB maximum document before unbounded allocation;
- object root;
- duplicate-key rejection at every nesting level;
- rejection of `NaN`, positive infinity, and negative infinity;
- no more than 512 accounts;
- labels preserved without Unicode normalization, non-empty, without control
  or NUL characters, and no more than 512 encoded UTF-8 bytes;
- non-empty token values no larger than 256 KiB;
- diagnostic strings no larger than 4 KiB; and
- the exact per-field bounds below.

| Field or collection | Exact limit |
|---|---:|
| Complete document | 16 MiB |
| Accounts | 512 entries |
| Account label | 1-512 UTF-8 bytes; no Unicode controls |
| Access, refresh, or ID token | 1-262,144 UTF-8 bytes |
| Provider account ID | 1-4,096 UTF-8 bytes |
| Plan | 1-256 UTF-8 bytes |
| Scopes | 128 entries |
| One scope | 1-4,096 UTF-8 bytes |
| Codex auth-home string | 1-32,768 UTF-8 bytes |
| Opaque Codex last-refresh string | 1-4,096 UTF-8 bytes |
| Diagnostic error | 1-4,096 UTF-8 bytes |
| Heartbeat target list | 32 entries |
| One heartbeat target ID | 1-256 UTF-8 bytes |
| Heartbeat reset map | 32 entries |
| One reset-map key | 1-256 UTF-8 bytes |
| Historical timestamp input | 20-32 ASCII bytes |

Present strings are non-empty. Containers reject mixed element types and
duplicates where their representation can express duplicates. Null remains
distinct from a known-empty scope or heartbeat-target collection.

Generation-zero Sidekick timestamps accept exactly
`YYYY-MM-DDTHH:MM:SS[.f{1,6}](Z|+00:00)`. Parsing validates the calendar,
rejects leap seconds, requires UTC, and accepts years 0001 through 9999.
Claude expiry milliseconds are in `0..253402300799999`; Codex expiry seconds
are in `0..253402300799`. Boolean is never an integer, and every reverse
conversion must be exact at provider precision.

Generation zero enforces the same provider discriminator as version one.
Migration rejects provider-incompatible non-null values and never discards
them by silently replacing them with null.

Root dispatch treats a strict integer `schema_version` member as an envelope.
Integer `1` requires exactly `schema_version` and `accounts`; any other integer
is an unknown future schema and never falls back. An object-valued generation-
zero account label literally named `schema_version` remains eligible for the
generation-zero schema. Prototype dispatch is never attempted at the
authoritative Sidekick path.

Serialization uses UTF-8, `ensure_ascii=False`, two-space indentation, LF on
every platform, one trailing newline, fixed envelope/field order, and account
insertion order. It does not sort labels.

Backup equivalence is exact byte equality with a matching digest-derived
filename. Validated semantic equality is reserved for later canonical-versus-
compatibility location reconciliation and never authorizes backup overwrite.

##### Durable artifacts

For authoritative account path `<accounts>`, persistence owns only these exact
sibling artifacts:

| Artifact | Naming contract | Contains credentials | Lifecycle |
|---|---|---:|---|
| Lock | `<accounts>.lock` | No | Persistent sidecar; never migration-state evidence |
| Generation-zero backup | `<accounts>.v0.<sha256>.bak` | Yes | Immutable until full reset |
| Version-one rollback snapshot | `<accounts>.v1.<sha256>.bak` | Yes | Immutable until full reset |
| Prototype receipt | `<accounts>.prototype.<sha256>.receipt` | No | Immutable and retained across reset |
| Temporary | `.<accounts>.<purpose>.<32-lowercase-hex>.tmp` | Possibly | Never authoritative; cleanup under the lock only |

The digest is 64 lowercase hexadecimal characters over exact bytes. Artifact
discovery accepts only this grammar and never touches a glob result outside the
injected account parent.

Temporary `purpose` is exactly `authority`, `backup`, `snapshot`, or `receipt`.
The random component is 128 bits from `secrets.token_hex(16)`. A partially
matching, uppercase, malformed, or unknown basename is foreign and is never
opened, cleaned, or deleted by Sidekick.

The prototype receipt is deterministic UTF-8 JSON:

```json
{
  "receipt_version": 1,
  "prototype_sha256": "<64-lowercase-hex>",
  "target_schema_version": 1
}
```

It uses two-space indentation, LF, and one trailing newline. The digest in the
receipt filename is the exact prototype digest, not a digest of receipt bytes.

Before any generation-zero rewrite:

1. acquire the persistence lock;
2. open the source without following the final object and require a regular,
   protected, single-link file;
3. bounded-read, validate, and fingerprint the exact source;
4. derive the immutable backup filename from SHA-256;
5. if that backup exists, require exact protected byte equality;
6. otherwise create a private same-directory temporary copy;
7. write or copy, flush, synchronize, reopen, and digest-verify it;
8. publish it through an atomic no-replace operation;
9. harden the namespace through the qualified platform action; and
10. reopen and revalidate the published backup.

No non-equivalent, unreadable, unsafe, or malformed backup is overwritten,
renamed, or deleted automatically.

Authoritative commit then:

1. validates the complete transformed document in memory;
2. serializes deterministic bytes;
3. creates a private same-directory temporary file;
4. writes, flushes, synchronizes, reopens, and validates it;
5. revalidates source identity and exact digest;
6. replaces through the qualified platform primitive;
7. hardens the namespace;
8. reopens the authoritative path without following the final object; and
9. verifies exact bytes, current schema, identity, and permissions before
   reporting success.

A failure after replacement but before confirmed hardening is
`durability_uncertain`. Sidekick never performs a blind reverse write; a fresh
assessment selects the actually observed validated authority.

##### Platform behavior

On Linux, POSIX systems, and WSL's Linux filesystem, Sidekick operates through
a securely opened parent directory and descriptor-relative names:

- new state directories use `0o700`;
- credentials, backups, temporaries, and locks use `0o600` from creation;
- file data is flushed and synchronized before publication;
- atomic `link()` publishes a no-replace backup;
- `rename()`/`os.replace()` commits authoritative replacement;
- the parent directory is synchronized after publication, replacement,
  cleanup, and credential deletion; and
- final symlinks, non-regular files, and unexpected link counts fail.

Deletion holds the exact validated victim descriptor across the
descriptor-relative unlink. Success requires the held inode's link count to
fall by exactly one and the deleted basename to remain absent. A private-tree
directory deletion likewise proves that the held directory, rather than a
same-name replacement, became unlinked. Namespace replacement never produces
false reset success.

macOS follows that namespace protocol and requests `F_FULLFSYNC` for account
files and backups when available. An I/O failure is terminal; it is not treated
as absence of the feature.

Windows uses pywin32 so Sidekick does not reproduce security-critical native
signatures with local `ctypes`:

- `CopyFileW(..., TRUE)` creates a private security-preserving backup copy;
- `MoveFileExW` without replace publishes the immutable backup;
- `ReplaceFileW` with flags `0` replaces an existing authority while
  preserving its DACL and selected metadata;
- `FlushFileBuffers` hardens file data;
- `win32security` assesses owner, SIDs, inheritance, and effective DACL; and
- every partial/sharing failure triggers full candidate reassessment.

`REPLACEFILE_WRITE_THROUGH` is unsupported and is never requested or claimed.
Windows creates a private write-through temporary, flushes and verifies it,
replaces or publishes it through the operation appropriate to its role,
reopens the final object without following a reparse point, flushes the final
handle, and verifies bytes, identity, regular-file state, and DACL. Any failure
after replacement but before that proof returns `durability_uncertain`.
This is a documented best-effort protocol, not a fabricated Windows directory
`fsync` equivalent.

The accepted Windows DACL allows the current user, LocalSystem, and
Administrators. A null DACL, unassessable owner/inheritance, reparse-point final
object, or broad credential read/write for Everyone, Anonymous, Guests,
Authenticated Users, or Builtin Users fails closed. Enterprise inheritance
that cannot be classified receives repair guidance and is never stripped
silently.

Released v0.6 installations may have current-user-owned application and
private-Codex root directories at `0755` even though credential files and
per-account leaves are private. Passive assessment fails closed and never
changes them. Doctor reports the structured next command
`sidekick-usages permissions repair`; it does not prescribe recursive shell
permission changes.

The confirmed repair operation preflights the exact owner-controlled local
application root, rejects group/other-writable or linked/reparse state, holds
and identity-verifies the object while applying `0700` or the exact protected
Windows DACL, and then acquires the normal persistent account lock. Under that
lock it revalidates the account parent, repairs only the complete validated
private credential tree, performs a strict rescan, and reassesses persistence.
It preserves credential bytes and unrelated children. Windows uses the
adopted pywin32 security boundary and exact current-user, LocalSystem, and
Administrators SIDs; it never shells out to `icacls` or `Set-Acl`.

Stable source identity is `(st_dev, st_ino)` from the open final handle on
POSIX and Windows, combined on every platform with SHA-256 over bounded exact
bytes. Identity detects replacement; digest detects content change.

The first writer supports only this closed local-filesystem allowlist:

| Host | Allowed authority filesystem |
|---|---|
| Native Linux | ext4, XFS, or Btrfs |
| macOS | APFS |
| Native Windows | NTFS |
| WSL | ext4 inside the Linux distribution |

It rejects NFS, SMB/CIFS, network FUSE, UNC, WSL 9p/DrvFS, tmpfs, overlayfs,
FAT, exFAT, ReFS, cluster/shared volumes, cross-device paths, and unknown
filesystem names. Linux and WSL resolve the longest matching
`/proc/self/mountinfo` entry, decode its escapes, and compare it with the open
directory identity. macOS uses its native report for the open directory;
Windows requires a local NTFS volume with persistent ACL support. Unsupported
hard locks, unassessable permissions, or unavailable synchronization fail
closed.

##### Explicit migration and rollback surfaces

`persistence/migrations/service.py` is the sole assessment and execution
owner.
`AccountStore.load()` is read-only with respect to migration and accepts only
the current schema. Composition assesses before constructing the store.

Normal command behavior is:

| Assessment | Behavior |
|---|---|
| No candidate | Empty current in-memory store; first authorized persist creates version one |
| Current version one | Construct `AccountStore` |
| Generation zero | `migration_required` with exact command |
| Eligible prototype only | `prototype_import_required` with exact command |
| Malformed, unreadable, unsafe, conflicting, or partial | Fail closed with doctor action |
| Unknown future version | `future_schema`; require compatible software |

Help and version bypass assessment. Doctor consumes a read-only validated
account snapshot from persistence and never constructs `AccountStore`, even
for empty or current state.

The authorized surfaces are:

```text
sidekick-usages migrate accounts [--yes] [--reimport-prototype]
sidekick-usages migrate prepare-rollback --target v0.6.0 [--yes]
sidekick-usages permissions repair [--yes]
```

These mutation commands require the installed Sidekick scheduler to be
stopped, print only safe path/generation/count/artifact data, and require
terminal confirmation unless `--yes` is explicit. Migration and rollback
acquire the normal lock before mutation. Permission repair uses only the
bounded owner-controlled bootstrap needed to make the released account parent
lockable, then acquires the same normal lock before any credential-tree repair.
Prototype reimport is never automatic and requires the explicit option and
confirmation.

The lock budget is exactly five seconds with a 100 ms check interval. The
filesystem adapter securely creates or opens and validates the persistent
sidecar before Portalocker receives the open file object. Mutation never uses
Portalocker's high-level pathname opener. A timeout is `store_locked`.

Scheduler quiescence checks every backend that can coexist on the host:

- native Linux: systemd user timer and cron marker;
- macOS: launchd agent and cron marker;
- Windows: Task Scheduler task; and
- WSL: systemd user timer, cron marker, and Windows Task Scheduler task.

Stopped means no Sidekick-owned schedule is installed. An unassessable backend
blocks mutation. Preview and confirmation happen without holding the lock;
scheduler quiescence and the complete assessment are repeated under the lock
immediately before mutation.

Rollback preparation supports exactly the released v0.6.0 target initially:

1. validate latest version one;
2. run the pure v0.6 compatibility preflight, rejecting explicit empty
   heartbeat target/reset collections without mutation;
3. publish its exact content-addressed v1 snapshot;
4. reverse every representable field to the strict v0.6.0 generation-zero
   representation;
5. commit that generation zero through the durable protocol;
6. run the actual isolated v0.6.0 reader against the exact file; and
7. report `rollback_prepared` and the safe snapshot basename.

After preparation, generation zero is authoritative. If v0.6.0 changes it, a
later re-upgrade migrates that current file and never silently restores a
retained version-one snapshot. Content-addressed artifacts make repeated
upgrade/downgrade cycles idempotent.

After native relocation, rollback preparation additionally materializes the
latest reverse document at the v0.6.0 compatibility path and asks the injected
`PrivateAuthMigrator` to validate and copy every Sidekick-owned Codex bundle
before account paths commit. External/provider-native homes remain unchanged.
Distinct compatibility and canonical roots are locked in deterministic
resolved-path order. Because the released writer does not honor the new lock,
the retained compatibility authority is fingerprinted and revalidated
immediately before and after canonical authority commit. A detected released
writer race is a typed conflict or resumable partial state, never silent
success. Native relocation reuses the CS-18 multi-bundle journal so every
bundle and the rewritten authority recover as one unit.

Ordinary credential recovery remains strict when its retained source guard
changes. Native migration adds a separate capability that can resolve a race
with the released writer without weakening that runtime contract. The
migration service supplies the freshly validated current compatibility
fingerprint and the exact original source path. The transaction first proves
that the source-path digest matches the journal, then classifies the canonical
authority as base, target, or third before changing anything. A base authority
rolls every private target mutation back and, when the base is present,
publishes that exact base as its content-addressed lineage snapshot. A target
authority rolls the tuple forward, removes only journal-listed displaced
target bundles, and publishes the exact canonical target as its lineage
snapshot. An absent base needs no artificial marker. A third authority, wrong
path, missing or malformed journal, private third state, or unprovable current
source fails closed with all recovery evidence retained.

The divergent-source resolver never writes or deletes the compatibility
source. It revalidates that source around cleanup and returns a closed
`source_diverged_base` or `source_diverged_target` outcome only after complete
target coherence. A crash after target cleanup remains resumable from the
lineage snapshot. While both locks remain held and scheduler quiescence still
holds, the migration service may perform at most one fresh reassessment and
one rebase from the current valid compatibility authority. A second race
retains the new journal and exits as partial or conflicting state; it never
enters a retry loop.

The runtime `PrivateCredentialTransaction.commit()` API remains version-one
only. CS-19 adds a separately named migration commit whose target generation
is explicit and payload-validated. Credential journal version two records a
base generation coherent with absent/present base authority plus the target
generation. Divergent-source recovery accepts only version-two journals. The
decoder continues to accept version-one journals with an implicit version-one
target for ordinary strict recovery. Only `persistence/migrations/service.py`
may invoke the migration commit.

##### Prototype receipt and reset

A successful prototype import publishes a validated non-secret receipt whose
name contains the exact prototype digest. The prototype remains unchanged.
Authority is committed and verified before the receipt is published. A crash
after authority commit but before receipt publication is `current`; an
idempotent explicit rerun may publish the missing receipt when the exact
prototype-to-v1 relation still holds. Publishing the receipt first is
prohibited because it could suppress the only import source without an
authority.

Full reset is a credential-destruction transaction under the lock. It removes:

- the authoritative account document;
- every valid Sidekick-managed v0/v1 account backup or snapshot;
- owned secret-bearing account temporaries; and
- every Sidekick-owned private Codex bundle in the bound private root.

It retains the non-secret lock and prototype receipts and never deletes the
external prototype. If any credential artifact cannot be removed, reset returns
`reset_incomplete` and does not claim all accounts were deleted.

Reset deletes credential backups and secret temporaries before deleting the
authority last. After restart, authority still present is assessed normally;
authority absent with credential artifacts is `interrupted_artifacts` even
when the immediate failed operation returned `reset_incomplete`.

A provider-scoped reset cannot delete shared historical backups without
destroying the other provider's recovery state. Only full reset, or a separately
approved explicit prune workflow, removes those shared historical copies.

All Sidekick-owned private credential writes, permission repair, and deletion
participate in the same persistent account lock. A writer cannot recreate a
bundle between reset's absence proof and authority-last deletion.

After reset, a matching receipt prevents stale prototype credentials from
reappearing. A changed prototype is reported as available but still requires
explicit `--reimport-prototype`.

A valid version-one authority without a generation-zero backup is `current`.
That is a legitimate first persist from empty. The assessor may report whether
matching history exists, but it never invents whether missing history means a
first write or a deleted backup. Rollback remains lossless for representable
state because preparation snapshots and reverses the current version-one
authority; explicit empty heartbeat collections fail compatibility preflight
before that snapshot.

##### Assessment and error vocabulary

Persistence owns a closed assessment vocabulary:

```text
empty
current
migration_required
prototype_import_required
prototype_imported
malformed_json
duplicate_key
invalid_schema
future_schema
unreadable
unsafe_permissions
unsupported_filesystem
store_locked
source_changed
backup_conflict
interrupted_artifacts
replace_failed
durability_uncertain
legacy_writer_detected
rollback_required
rollback_prepared
reset_incomplete
```

Passive assessment is a phased reduction: qualify the filesystem; enumerate
only exact managed names; validate object security; bounded-read; decode;
verify digest-derived names; derive relations; then sort every issue. Unknown
sibling names are never opened or reported. Lower priority numbers win, while
all safe findings remain available in deterministic order:

| Priority | Code |
|---:|---|
| 10 | `unsupported_filesystem` |
| 20 | `unsafe_permissions` |
| 30 | `unreadable` |
| 40 | `duplicate_key` |
| 50 | `malformed_json` |
| 60 | `future_schema` |
| 70 | `invalid_schema` |
| 80 | `backup_conflict` |
| 90 | `interrupted_artifacts` |
| 100 | `legacy_writer_detected` |
| 110 | `rollback_prepared` |
| 120 | `migration_required` |
| 130 | `prototype_import_required` |
| 140 | `prototype_imported` |
| 150 | `current` |
| 160 | `empty` |

Within one code, issues order by authority, lock, sorted v0 basenames, sorted
v1 basenames, sorted temporary basenames, sorted receipt basenames, then
prototype. Account insertion order is never sorted.

The core authority reduction is exact:

| Authority | Additional evidence | Result |
|---|---|---|
| Absent | Any credential backup or owned temporary | `interrupted_artifacts` |
| Absent | No credential artifact | Prototype/receipt matrix |
| Generation zero | Owned temporary | `interrupted_artifacts` |
| Generation zero | A v1 snapshot reverses exactly | `rollback_prepared` |
| Generation zero | V1 snapshots exist; none reverses exactly | `legacy_writer_detected` |
| Generation zero | No v1 snapshot | `migration_required` |
| Version one | Owned temporary | `interrupted_artifacts` |
| Version one | Exact prototype/receipt/import equality | `prototype_imported` |
| Version one | Otherwise | `current` |
| Future, duplicate, malformed, invalid, unreadable, unsafe | Any | Corresponding closed code |

Multiple different valid backups are expected history. A v0 backup must have
protected exact bytes matching its digest name and decode as generation zero;
a v1 snapshot additionally must be canonical version-one bytes. Unsafe backup
objects are `unsafe_permissions`, unreadable ones are `unreadable`, and digest,
generation, malformed-content, or canonical-content failures are
`backup_conflict`. Historical backups never become authority automatically.

Prototype fallback is considered only when authority, credential backups, and
owned temporaries are absent. A receipt matching exact prototype bytes
suppresses reimport and yields `empty`; no receipt yields
`prototype_import_required`; only nonmatching historical receipts require the
explicit `--reimport-prototype` option. With v1 authority,
`prototype_imported` is restart-derived only when valid prototype bytes,
matching receipt, and deterministic transformed v1 bytes all match exactly.

The public passive result is a frozen, slotted `PersistenceAssessment` carrying
the primary code, generation, schema version, validated count, safe path, safe
artifact basename, write-blocked flag, structured next-command tuple, bounded
message, and an ordered tuple of equally safe `PersistenceIssue` values. A
mutation returns `PersistenceOperationResult(code, assessment, artifact,
message)` so a transient outcome never replaces the freshly observed state.

Doctor exits `0` for `empty`, `current`, `prototype_imported`, and intentional
`rollback_prepared`; exits `1` for migration, prototype import, legacy-writer,
unambiguous interruption, and future-version action; and exits `2` for schema,
I/O, security, filesystem, and backup-integrity failures. Scheduler
quiescence retains exit `3`. Normal composition accepts only `empty`,
`current`, and `prototype_imported`.

`store_locked`, `source_changed`, `replace_failed`, `durability_uncertain`,
`reset_incomplete`, and `rollback_required` are operation-time facts. They are
not fabricated after restart. Restart can derive exact rollback/prototype
relations and snapshot-proven legacy writes, but cannot derive a lost flush,
prior lock, completed replace call, reset intent, ordinary v1-to-gen0 overwrite
without v1 proof, or first-write provenance.

Durable checkpoints reduce as follows:

| Workflow checkpoint | Restart result |
|---|---|
| Gen0 before backup | `migration_required` |
| Matching v0 backup published | Resumable `migration_required` |
| Migration output temporary exists | `interrupted_artifacts` |
| V1 replacement verified | `current` |
| Prototype before V1 commit | `prototype_import_required` |
| Prototype V1 temporary exists | `interrupted_artifacts` |
| Prototype V1 committed before receipt | `current` |
| Prototype receipt temporary exists | `interrupted_artifacts` |
| Receipt published with exact import relation | `prototype_imported` |
| Rollback before snapshot | `current` |
| Rollback snapshot published while V1 remains authority | `current` |
| Rollback gen0 temporary exists | `interrupted_artifacts` |
| Gen0 exactly reverses retained v1 snapshot | `rollback_prepared` |
| v0.6 later changes that gen0 | `legacy_writer_detected` |
| Re-upgrade commits V1 | `current` |
| Normal persist before temporary | `current` |
| Normal persist candidate temporary exists | `interrupted_artifacts` |
| Normal persist replacement verified | New `current` |
| Reset while authority remains | Existing logical state |
| Reset removes authority while credentials remain | `interrupted_artifacts` |
| Reset removes credentials and retains matching receipt | `empty` |
| Prototype changes after retained historical receipt | Import required with `--reimport-prototype` |

A post-replacement hardening failure returns immediate
`durability_uncertain`; restart reports only the state it can validate.

Human and JSON output expose only the owned code, known generation, safe path,
validated account count, safe artifact basename, write-blocked flag, exact next
command, ordered safe issues, and bounded Sidekick-authored message. Tokens,
raw records, full
provider identities, raw validation errors, native exceptions, and third-party
exception graphs never cross the boundary.

##### Persistence dependency and module ownership

The dependency decision is:

| Option | Fit | Decision |
|---|---|---|
| Portalocker 3.2.0 | Mature cross-platform hard lock; fail-closed native behavior behind an owned adapter | **GO** |
| filelock 3.29.7 | Strong maintenance and Python 3.14 support, but Unix can fall back to `SoftFileLock` on `ENOSYS` | NO-GO for this boundary |
| pywin32 312 | Maintained CPython 3.14 native Windows file and DACL bindings | **GO on Windows** |
| types-pywin32 312.0.0.20260609 | Matching Windows development stubs; native returns still require owned narrowing | **GO for Windows development** |
| `atomicwrites` | Archived, old, and insufficient Windows/durability behavior | NO-GO |
| Portalocker `open_atomic()` | Create-only; no replacement, namespace hardening, ACL, source comparison, or recovery states | NO-GO as writer |
| SQLite | Robust exemplar but incompatible with the approved JSON/v0.6.0 contract | NO-GO as storage |

Declare exact reviewed dependencies:

```text
portalocker==3.2.0
pywin32==312; sys_platform == "win32"
```

Declare `types-pywin32==312.0.0.20260609` as a Windows-only development
dependency. Stub incompleteness never justifies `Any`, an unchecked cast, or a
blanket suppression.

pywin32 is direct because Sidekick consumes its APIs independently of
Portalocker's Windows closure. Both remain private to `persistence/`.

The cohesive package owns:

```text
persistence/
├── __init__.py
├── account_store.py
├── errors.py
├── filesystem.py
├── locking.py
├── migrations.py
├── schemas.py
└── _platform/
    ├── __init__.py
    ├── macos.py
    ├── posix.py
    └── windows.py
```

`filesystem.py` owns only qualified commit, synchronization, identity, and
permission behavior. `locking.py` owns only Portalocker acquisition and
Sidekick error translation. They are concrete infrastructure boundaries, not
generic utilities. `_platform/` owns the three genuinely different native
implementations and does not become a general filesystem toolkit. No
third-party type crosses `persistence/__init__.py`.

##### Required proof

The few load-bearing suites are:

1. one table-driven lexical/schema suite covering prototype, every historical
   generation-zero field set, exact version one, duplicate keys, non-standard
   constants, strict types, extras, bounds, and future versions;
2. one pure forward/reverse suite proving deterministic output and lossless
   v0.6.0 representation;
3. one parameterized interruption suite over source validation; backup
   temporary create/write/sync/publish/harden; output temporary
   create/write/sync; source revalidation; replace return; namespace hardening;
   and final reopen/validation;
4. one real two-process lock/source-change suite with a simulated
   non-participating legacy writer;
5. one permission/link/filesystem suite per supported native platform;
6. one reset/prototype-receipt suite proving deleted credentials never
   reappear and partial deletion is not success;
7. one actual-v0.6.0 repeated upgrade/downgrade harness with a change written
   on both sides; and
8. one human/JSON doctor secrecy table for every assessment state.

The schema suite pins every exact numerical bound and timestamp/range edge.
The state-machine suite pins precedence, multi-issue ordering, prototype and
receipt reduction, first-write v1 without history, backup relations, operation
versus restart facts, doctor exit mapping, and every durable transition table.
The lock suite proves the five-second/100 ms policy without sleeping in unit
tests. The scheduler suite proves every coexisting backend is absent or blocks
mutation.

Every interruption test starts a fresh assessment and authorized resume. It
asserts one unambiguous authority, no accepted partial artifact, preserved
source bytes, idempotent final bytes, and no empty-store fallback.

Native evidence uses the exact built wheel and final dependency lock on local
Linux, macOS/APFS, Windows/NTFS as a normal user, and WSL2's Linux filesystem.
It records OS, filesystem, Python, dependency versions, wheel hash, and result.
A hosted Linux pass is not evidence for macOS, Windows, or WSL.
The writer remains disabled until Linux descriptor-relative operations,
macOS `F_FULLFSYNC`, Windows pywin32/DACL/final-flush behavior, WSL ext4
acceptance and 9p rejection, exact-wheel contents, and the actual-v0.6.0
two-cycle compatibility harness all pass.

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
- transport retries disabled per request and Sidekick-owned closed retry
  policies.

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

### 10.3 Completed build-versus-adopt research

**Decision:** **GO — OPERATOR APPROVED 2026-07-10**. Use urllib3 2.7.0 for
pooled transport with its retries disabled, plus one focused Sidekick executor
as the sole retry owner. The approval authorizes the CS-11 dependency and
production refactor subject to every implementation and release gate below.
The self-contained evidence record is
[HTTP Transport and Retry Dependency Research][http-research].

| Option | Completed result | Research disposition |
|---|---|---|
| Standard library plus focused executor | Retains the current non-pooled transport and owned connection behavior | NO-GO; pooling is mandatory |
| urllib3 `PoolManager` plus urllib3 `Retry` | Strong HTTP classifications and guidance parsing, but directly owns wall time, sleep, and randomness and has no whole-operation deadline | NO-GO as retry owner |
| Retry-disabled urllib3 plus Tenacity 9.1.4 | Injects sleep and can retain a final response, but directly timestamps retry state and leaves the operation matrix, HTTP-date parsing, deadline, and typed translation local | NO-GO for the current contract |
| Retry-disabled urllib3 plus focused executor | One pooled transport and direct injection of the two clocks, sleeper, RNG, operation policy, and terminal outcome | **GO — approved** |
| HTTPX and Stamina controls | Add broader sync/async, protocol, or observation surfaces without removing the product-specific policy | NO-GO for current requirements |

The approved selection is not a failure to reuse. urllib3 buys the maintained
TLS,
pool, timeout, response, and proxy transport surface. Sidekick owns only the
closed retry semantics that no candidate supplied without brittle subclassing
or a second local policy layer. Three current loops and six concrete operation
classes satisfy the rule of three; arbitrary hooks, provider-created policies,
circuit breakers, async support, and a generic resilience service do not.

The transport contract is:

- one invocation-scoped `PoolManager` and, when required, `ProxyManager`;
- `retries=False` and `redirect=False` at manager configuration and on every
  request;
- one focused executor in `http/retry.py` as the only attempt owner;
- independently injected aware UTC wall time, monotonic elapsed time, sleeper,
  and deterministic test RNG;
- environment proxy and `NO_PROXY` behavior preserved explicitly because
  urllib3 does not reproduce the standard-library opener automatically;
- system CA behavior and verified HTTPS preserved without an unapproved
  certificate dependency; and
- no urllib3 response, exception, request object, proxy credential, token,
  authorization header, body, or full account identity above `http/`.

The isolated package added one dependency-free pure-Python distribution,
432,560 installed bytes, and one Homebrew resource. Only Linux CPython 3.14.6
ran the local server and timing work. Native proxy, CA, TLS, Homebrew, macOS,
Windows, and WSL behavior remain implementation gates rather than inferred
success.

Reopen the decision if urllib3 fails a supported platform, security,
provenance, packaging, proxy, or CA gate; a provider documents an idempotency
key or authoritative not-applied signal; real async, HTTP/2, cancellation, or
multiple retry-domain requirements emerge; or the focused executor begins
recreating a general-purpose retry framework. Do not silently stack a second
retry owner.

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

- `SAFE_READ`;
- `CLAUDE_PROBE`;
- `CLAUDE_REFRESH`;
- `CODEX_REFRESH`;
- `CLAUDE_HEARTBEAT`; and
- `CODEX_HEARTBEAT`.

Do not expose `retry: bool`, arbitrary policy injection, or provider-created
retry configuration.

The completed safety classification is normative under the approved CS-08
decision. **R** means retry within both attempt and monotonic elapsed bounds;
**T** means a terminal typed outcome; **R\*** means retry only when the complete
valid server delay fits the remaining deadline.

| Operation | Proven pre-send connect | Ambiguous read or protocol | 429 | Selected 5xx | Explicit rejection |
|---|---:|---:|---:|---:|---:|
| `SAFE_READ` | R | R | R\* | R: 500, 502, 503, 504; Claude 529 | T |
| `CLAUDE_PROBE` | R | T | R\* | T | T |
| `CLAUDE_REFRESH` | R | T | T with guidance | T | T |
| `CODEX_REFRESH` | R | T | T with guidance | T | T |
| `CLAUDE_HEARTBEAT` | R | T | R\* | T | T |
| `CODEX_HEARTBEAT` | R | T | T with guidance | T | T |

Only urllib3's narrow connect category documented as occurring before the
request is sent proves a POST was not applied. A read or protocol failure can
follow a sent request and is terminal for every current POST. Claude Messages
429 is an authoritative rejection and may retry within bounds. No
endpoint-specific idempotency key or not-applied signal was found for either
OAuth refresh or the provider-private Codex heartbeat endpoint, so their 429
and 5xx results remain terminal while retaining user guidance. This matrix is a
conservative safety inference from standards and the available provider
contracts, not a claim that the endpoints are idempotent.

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

### 10.7 Completed research and implementation acceptance gate

The 2026-07-10 spike exercised all four request shapes and the current typed
error boundary against exact-version candidates. Its decision-critical results
were:

| Probe | Result |
|---|---|
| Request capabilities | GET object, POST JSON object, POST form object, and POST bytes-to-headers all passed |
| Pool reuse | Six local requests used one observed client source port |
| Final rate limit | The terminal 429 response and last valid guidance remained available |
| `Retry-After` | Integer and HTTP-date forms, past dates, malformed values, and a cap were exercised |
| Deadline | Injected monotonic time stopped at attempt two while a five-attempt budget remained because the next delay exceeded the elapsed deadline |
| urllib3 package | 432,560 installed bytes, no mandatory dependency, one Homebrew resource |
| urllib3 plus Tenacity | Added 86,532 bytes and a second resource while leaving the HTTP policy local |
| Platform execution | Linux CPython 3.14.6 only; other-platform metadata is not runtime proof |

The local server proved request encoding, response retention, connection reuse,
and deterministic time control. It did not prove remote TLS session reuse,
environment proxies, CA-store integration, provider behavior, or native macOS,
Windows, and WSL behavior. CS-11 must satisfy those gaps before merge and must
measure real help/version, one-account, and representative multi-account
startup. Help and version must not initialize the pool.

The implementation test set is deliberately small and load-bearing:

1. One local-server test exercises all four capabilities, connection reuse,
   bounded reads, and deterministic closure.
2. One parameterized operation-safety test proves the six policies across
   connect, ambiguous read, 429, selected 5xx, and explicit rejection by
   asserting attempt count and terminal typed outcome.
3. One `Retry-After` table proves integer, future-date, past-date, malformed,
   cap, last-valid retention, and delay-beyond-deadline behavior.
4. One synthetic-deadline test injects both clocks, sleeper, and RNG and proves
   the elapsed bound stops retry while attempts remain.
5. One typed-boundary table covers non-HTTPS rejection before transport, auth,
   forbidden diagnostics, rate-limit guidance, exhaustion, malformed or
   non-object JSON, and oversized response behavior.
6. One credential-safety test places distinct sentinels in authorization,
   body, proxy URL, and account identity and proves they are absent from every
   public error and retry observation.
7. One composition/proxy test proves environment proxy and `NO_PROXY`
   selection, request-level redirect rejection, lazy initialization,
   deterministic closure, and unchanged JSON, quiet, and scheduled stdout.

Parameterize related outcomes rather than copy the transport setup. Preserve
existing tests only when they protect distinct behavior. Do not assert urllib3
internals, complete third-party error strings, full renderer snapshots,
projected module line counts, or a coverage number without a behavior contract.

The CS-08 GO approves the retry-disabled urllib3 plus focused-executor pair and
its exact safety matrix. It does not waive the proxy, CA, TLS, platform,
packaging, redaction, or lifecycle acceptance gates.

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
| Aware-UTC runtime normalization | Expiry, account runtime/reset, usage, heartbeat | `core/time.py:as_utc` |
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

- apply the approved Pydantic boundary-validation decision;
- create top-level `paths.py` with frozen `ApplicationPaths`;
- centralize the exact current Sidekick-owned locations without relocating
  existing files;
- inject concrete paths from the current composition root (`cli.py` during
  intermediate phases, then `cli/app.py` after the atomic CLI package
  conversion) into their consuming adapters and services;
- apply the approved `platformdirs` discovery contract without relocation;
- establish `core/models.py`, `core/types.py`, `core/expiry.py`, and the narrow
  `core/time.py` aware-UTC normalizer;
- establish `clock.py` and inject aware wall time into application services;
- move shared models without changing behavior;
- move provider-neutral expiry classification into core;
- replace shared timestamp strings with aware datetimes and boundary-local
  persistence, provider, and presentation encoding;
- establish JSON and boundary schemas;
- apply the approved urllib3 and focused-executor HTTP decision;
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
Compatibility and canonical locks are acquired in deterministic resolved-path
order, all sources are revalidated while held, and the compatibility authority
is fingerprint-checked around the canonical commit to detect a concurrent
released writer that does not understand the new lock.

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

- creates `cli/__init__.py`, `app.py`, `context.py`, `help.py`,
  `token_input.py`, and the complete `commands/` tree;
- moves every complete command cluster and its private helpers to the exact
  owner in section 8.5;
- updates tests to patch the owning module or inject dependencies;
- preserves `sidekick_usages.cli:app`;
- preserves `python -m sidekick_usages`;
- deletes the old top-level `cli.py`, `cli_help.py`, and `token_input.py` in the
  same atomic change; and
- verifies source, sdist, and wheel contain the package and no stale flat
  module.

`cli/__init__.py` exports only `app` and `run`. The installed console entry
point remains `sidekick_usages.cli:app`, and `__main__.py` imports `run` from
that public facade. Packaging verification uses one freshly built wheel,
installs that exact artifact into an isolated environment, clears source-tree
import leakage, changes to a directory outside the checkout, and exercises
both the console script and `python -m sidekick_usages`. It also inspects wheel
and sdist members for every required `cli/commands/*.py` owner and rejects
`sidekick_usages/cli.py`, `cli_help.py`, and top-level `token_input.py`.

The conversion is not complete if editable-checkout imports pass while an
artifact contains both generations. No stale-file compatibility shim or broad
package re-export may hide a packaging error.

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
- `core/time.py` owns only aware-datetime validation and UTC normalization;
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
- `persistence/migrations/service.py` is the only durable-state
  location-migration
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

- the exact command ownership in section 8.5, with each current command
  registered once and no command module importing the global application;
- strict service-only `AppContext` plus the narrow persistence, doctor,
  daemon, and update contexts;
- the closed ready, blocked, and failed doctor states, with no store load for
  blocked or failed diagnostics;
- lazy typed `ctx.obj` access with no module singleton or `set_context()`;
- `Composed[T]` transfer, close-once behavior, partial LIFO cleanup, and
  original-error preservation;
- explicit empty provider and heartbeat registries remaining empty;
- narrow prompt, heartbeat display, lifetime, update, and Claude setup-token
  seams instead of command-facing raw infrastructure;
- no `cli.py`, `cli_help.py`, or top-level `token_input.py` in source, sdist,
  or wheel;
- `cli/app.py` at approximately 200 lines or fewer;
- a valid `sidekick_usages.cli:app` entry point;
- a functional `python -m sidekick_usages` path;
- registration-only `create_app()` and help rendering without account or
  credential initialization;
- help and version perform no application-path discovery, store loading,
  directory creation, scheduler construction, or HTTP initialization;
- discoverable Claude and Codex capabilities;
- every command module below the 800-line target;
- no Typer import from providers or services;
- no Rich import from core or application services;
- stable command discovery and help output;
- compatibility behavior matching the approved migration policy; and
- one isolated exact-wheel installation, executed outside the checkout with
  source leakage cleared, proving both public entry paths.

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
- a composer sentinel proving root, nested, and leaf help plus version invoke
  no `require_*()` accessor;
- partial composition closing acquired resources once in LIFO order while
  preserving the original construction error;
- repeated `Composed.close()` and repeated access closing each resource once;
- lazy `ctx.obj` access returning the same typed context on repeated use;
- `DoctorReady`, `DoctorBlocked`, and `DoctorFailed` producing exhaustive
  output, with blocked and failed states never constructing `AccountStore`;
- explicitly empty provider and heartbeat registries remaining empty through
  normal and doctor composition;
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
- token prompting only for the exact missing-credential outcome;
- prompt metadata, heartbeat display data, lifetime collection, update work,
  and Claude setup-token capture crossing only their narrow service seams;
- maintenance ordering and exit status;
- heartbeat scheduling and provider targets;
- usage partial success and typed failures;
- provider command groups and compatibility aliases;
- each current command registered once by its owning module;
- isolated source, sdist, and exact-wheel entry-point behavior with no stale
  flat CLI modules; and
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
Delete tests of `_ContextState`, `set_context()`, `_get_ctx()`, or a mutable
global context when the package conversion removes those surfaces. Replace
them with command behavior through injected `InvocationContext` composers.

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

Do not add tests that merely enumerate dataclass fields, import every new
module, assert that a registration function exists, or prove a fake was
called. Context tests must exercise lifecycle, lazy composition, blocked-state
safety, or command behavior. One parameterized context-access test is better
than five copied accessor tests when the behavior is identical. One packaging
test may inspect required module members because stale artifact contents are a
release contract, not an import-existence smoke.

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
explicit, typed `InvocationContext` harness only when at least three command
test modules still need it. It must not be autouse, mutate global state,
construct real resources, build every service for every test, or become a
general fake framework. Tests needing only one narrow context inject only its
composer.

Type `pytest.MonkeyPatch`, `Path`, fake methods, and helper return values. Keep
filesystem, provider, HTTP, wall-clock, monotonic-time, and scheduler
boundaries injectable. The fake application wall clock and HTTP monotonic timer
remain separate unless a concrete shared semantic contract emerges.
Do not introduce `Any` merely to make a fixture, mock, or helper convenient.

### 16.5 Architecture checks

Add focused checks for:

- no production module over 1000 lines;
- review warning near 800 lines;
- no `Any` or unjustified casts in application, core, or test code;
- no blanket suppressions;
- no CLI imports from core or providers back into commands;
- no Rich or Typer imports in services and core;
- no transport or retry-library imports outside `http/`;
- exactly one configured HTTP retry owner;
- no HTTP, external settings loader, operating-system path discovery,
  filesystem, or infrastructure import from `core/`;
- `core/expiry.py` remains pure and infrastructure-independent;
- `core/time.py` remains a pure `as_utc()` invariant with no parsing,
  formatting, clock acquisition, or boundary encoding;
- no production `timestamps.py` or universal timestamp-string formatter;
- no application-wide settings singleton;
- no declared runtime `pydantic-settings` dependency or production import
  without a separately approved settings contract;
- no `platformdirs` import outside `paths.py`;
- no provider-native or scheduler installation path in `ApplicationPaths`;
- no optional field, `ApplicationPaths`, wall-clock, raw HTTP client, provider
  registry, heartbeat registry, raw scheduler backend, console, filter, or
  result state in an operational context;
- `AppContext` contains only the approved store-backed services and narrow
  Claude setup-token capability;
- persistence, doctor, daemon, and update commands use their narrow context,
  not `AppContext`;
- `DoctorState` remains the closed `DoctorReady | DoctorBlocked |
  DoctorFailed` union;
- `create_app()` and command registration perform no runtime composition;
- help and version have no path, store, credential, scheduler, or HTTP side
  effect;
- an explicit empty provider or heartbeat registry is never replaced by
  defaults;
- each acquired invocation resource has one `Composed[T]` owner and one
  close-once registration;
- no duplicate Sidekick-owned `Path.home()` reconstruction;
- no import-time Sidekick path discovery;
- no durable-state location-migration coordinator outside
  `persistence/migrations/service.py`;
- no provider-package import from `persistence/migrations/`;
- the location-migration coordinator consumes the narrow
  `PrivateAuthMigrator` port instead of provider auth internals;
- no private-Codex root reconstruction outside `paths.py`;
- no direct `datetime.now()` or `time.time()` in application services,
  providers, or renderers after clock migration;
- no clock import from `core/expiry.py`;
- no provider and persistence timestamp serializer importing the other;
- exactly one `ApplicationPaths` owner for Sidekick-owned locations;
- exactly one canonical robot source;
- source, sdist, and built-wheel CLI package contents;
- all exact command modules from section 8.5 are packaged;
- no stale same-named module after package conversions; and
- `cli/app.py` remains near or below 200 lines, every cohesive module remains
  below the 800-line target where practical, and no production module exceeds
  the 1000-line hard limit.

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

The organizational distinctions and initial dependency assessment use primary
sources retrieved on 2026-07-09. Focused dependency research was refreshed on
2026-07-10 and is recorded separately below.

- **Repository commit:**
  `42cd01eb17c7903b385b1b4e259cf5b0c64126c5`
- **Python target:** 3.14
- **Current relevant runtime dependencies:** Click, Typer, and Rich
- **Validation baseline:** no current runtime validation dependency
- **Validation decision:** the section 6.4 spike is complete and Pydantic
  2.13.4 was operator-approved on 2026-07-10 with required release gates
- **HTTP baseline:** no current third-party HTTP or retry dependency
- **HTTP decision:** the section 10.3 spike is complete and urllib3 2.7.0 with
  retries disabled plus one focused executor was operator-approved on
  2026-07-10
- **Application-path baseline:** `store.py`, `cli.py`, and `lifetime.py`
  duplicate Sidekick-owned location construction; `store.py` also retains the
  separate `cc-usage` prototype source
- **Path dependency baseline:** `platformdirs` is not a declared runtime
  dependency; version 4.9.6 appears only transitively in the development lock
  graph through `python-discovery` and `virtualenv`
- **Application-path decision:** section 4.2 now contains the approved
  platformdirs 4.10.0 selection and exact path contract; relocation remains
  separately gated and disabled
- **Settings baseline:** the application has no cohesive multi-source settings
  contract
- **Settings decision:** `pydantic-settings` 2.14.2 is deferred and is not
  implied by a Pydantic schema-validation decision

### 20.1 CS-07 evidence snapshot

The 2026-07-10 validation research used repository snapshot
`c5b588ad474fd95c597cfd0b64339223e3da1843`, a synthetic eight-boundary
corpus, and eight malformed-input classes. Pydantic 2.13.4, cattrs 26.1.0,
msgspec 0.21.1, and a focused standard-library baseline were exercised on
Linux CPython 3.14.6. The exact measurements, candidate behavior, packaging
impact, limitations, sources, and conditional recommendation are in the
tracked [Schema Validation Dependency Research][schema-research].

Only Pydantic combined aggregate independent errors, exact account/list paths,
and stable machine-readable codes without coupling core models to a framework.
That evidence is counterbalanced by a measured five-distribution, 7,281,761
byte installed closure and an unproven Homebrew source build requiring Rust
and maturin. Package classifiers and wheels support the other operating
systems, but native macOS and Windows execution remains an implementation
gate. No dependency is approved by this evidence snapshot.

### 20.2 CS-08 evidence snapshot

The 2026-07-10 HTTP research used the same repository snapshot, exact tagged
urllib3 2.7.0 and Tenacity 9.1.4 sources, a loopback server, the four production
request shapes, and synthetic time. It exercised pooling, final responses, both
standard `Retry-After` forms, operation safety, and deterministic deadline
behavior. The complete evidence, primary sources, measured dependency cost,
limitations, security contract, and recommendation are in the tracked
[HTTP Transport and Retry Dependency Research][http-research].

urllib3 supplied the required pooled transport at one dependency-free
distribution and 432,560 measured installed bytes. Its `Retry` owner could not
meet independent clock, sleep, RNG, and whole-operation deadline requirements
without brittle subclassing or global patching. Tenacity injected sleep but
left nearly every HTTP-specific policy local. The focused Sidekick executor is
therefore the approved retry owner. Only Linux ran the local spike; native
proxy, CA, TLS, macOS, Windows, WSL, and Homebrew behavior remains an
implementation gate.

### 20.3 CS-09 evidence snapshot

The 2026-07-10 path research used the same repository snapshot, platformdirs
4.10.0 official APIs and tagged platform implementations, controlled native
class probes, and primary XDG, Apple, Microsoft, and WSL guidance. Exact
constructor arguments, platform outputs, current and native file roles,
override behavior, package evidence, activation gates, and reversal conditions
are in the tracked
[Application Path Discovery Dependency Research][paths-research].

The controlled probe returned the frozen Linux, absolute-XDG, macOS, Windows,
and WSL paths and created no filesystem object with `ensure_exists=False`.
platformdirs is one dependency-free pure-Python runtime distribution; native
operating-system probes and the full relocation/rollback transaction remain
later gates. The approved adoption authorizes discovery only. The current
compatibility account store, private Codex root, and lifetime cache remain
authoritative until an independently approved migration activates new
locations.

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

### 20.4 Dependency decision status

These later dependency decisions are separate from the approved 2026-07-09
baseline and are mirrored in the implementation-plan ledger.

| Change set | Research recommendation | State |
|---|---|---|
| CS-07 | Pydantic 2.13.4 `TypeAdapter` at untrusted boundaries; Homebrew source-build proof remains a release gate | **GO — APPROVED 2026-07-10** |
| CS-08 | urllib3 2.7.0 pooled transport with retries disabled plus one focused Sidekick retry executor | **GO — APPROVED 2026-07-10** |
| CS-09 | platformdirs 4.10.0 behind `paths.py`; physical relocation remains separately gated | **GO — APPROVED 2026-07-10** |

## 21. Approved decisions

The baseline decisions below were approved on 2026-07-09. The Pydantic,
urllib3/retry, platformdirs, and persistence-contract selections were approved
on 2026-07-10:

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
10. Use Pydantic 2.13.4 `TypeAdapter` at boundary-local schemas subject to the
    required Homebrew and platform release gates.
11. Preserve the installed entry point and machine-readable behavior.
12. Use the phased migration and load-bearing test strategy above.
13. Keep shared HTTP infrastructure outside `core/` in a dedicated `http/`
    package.
14. Use exactly one retry owner and require explicit POST operation safety.
15. Make pooling mandatory, use urllib3 2.7.0 with retries disabled, and use
    the focused Sidekick executor as the sole retry owner.
16. Make `core/expiry.py` the single owner of provider-neutral expiry
    classification.
17. Use `core/time.py:as_utc` only for shared aware-UTC runtime normalization,
    acquire wall time through the explicit `Clock`, and keep HTTP deadlines on
    an HTTP-local monotonic source.
18. Keep persistence, provider-native, and human timestamp encoding at their
    owning boundaries; do not create `timestamps.py` or a universal timestamp
    formatter.
19. Use frozen `ApplicationPaths` in top-level `paths.py`, construct it at the
    lazy CLI composition root, and inject concrete locations into their owners.
20. Keep provider-native homes and scheduler installation paths outside
    `ApplicationPaths`.
21. Use `platformdirs` 4.10.0 privately behind `paths.py` for native discovery,
    subject to the explicit platform, compatibility, and later migration
    gates.
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
26. Make `persistence/migrations/service.py` the sole durable-state
    location-migration coordinator, keep `paths.py` discovery-only, and
    consume an injected `PrivateAuthMigrator` port so provider auth semantics
    remain with provider adapters without introducing a
    persistence-to-provider import.
27. Use a strict, two-field schema-version-one account envelope and require an
    explicit migration command; normal store loading never rewrites an older
    schema or imports the prototype automatically.
28. Protect schema changes and downgrade preparation with immutable,
    content-addressed generation-zero and version-one snapshots, and use a
    non-secret content-addressed receipt to make prototype imports explicit.
29. Make rollback preparation for the actual v0.6.0 release lossless for every
    representable version-one state, fail before mutation for explicit empty
    heartbeat collections the pinned reader collapses to null, and verify the
    emitted generation-zero document with the released old reader before
    reporting success.
30. Adopt Portalocker 3.2.0 for the process lock and pywin32 312 on native
    Windows for qualified security and durability primitives; do not recreate
    those operating-system boundaries through local `ctypes` declarations.
31. Enable durable writes only on qualified local filesystems with assessable
    permissions, locking, synchronization, and replacement semantics; fail
    closed on unsupported or ambiguous environments.
32. Use a validating, non-normalizing `AccountLabel(str)` value object that
    preserves exact Unicode while enforcing the approved label bounds.
33. Nest the immutable `ClaudeCredentials | CodexCredentials` union inside a
    mutable `Account`, derive provider identity from the credential variant,
    and replace refreshed credentials atomically.
34. Store `KnownExpiry | UnknownExpiry | InvalidExpiry` and classify known
    instants into focused `ValidExpiry` or `ExpiredExpiry` results using one
    explicit aware UTC `now`.
35. Centralize only the proven cross-module `RefreshStatus`,
    `HeartbeatStatus`, and `ExpiryState` vocabularies; keep open and
    feature-local statuses out of core.
36. Normalize runtime expiry, audit, usage-reset, and heartbeat-reset values to
    aware UTC datetimes while preserving Codex auth `last_refresh` as the one
    bounded opaque provider-owned string.
37. Keep credential workflow models in `credentials/models.py`,
    provider-neutral orchestration in `credentials/service.py`, and the proven
    Codex auth-to-persistence bridge in `credentials/codex.py`; do not create a
    symmetric Claude module without a responsibility.
38. Make store-backed `AppContext` strict and service-only, and use separate
    typed `PersistenceContext`, `DoctorContext`, `DaemonContext`, and
    `UpdateContext` values for command families with different composition
    requirements.
39. Represent doctor composition as the closed `DoctorReady | DoctorBlocked |
    DoctorFailed` sum type so blocked and failed diagnostics never require a
    runtime store.
40. Transfer invocation resources through PEP 695 `Composed[T]` owners backed
    by `ExitStack`, register close exactly once through lazy `ctx.obj`
    accessors, and preserve the original construction failure if cleanup also
    fails.
41. Treat only `None` as a request for production registry defaults; preserve
    explicitly injected empty provider and heartbeat registries.
42. Use the narrow `TokenPromptSpec`, heartbeat display capability,
    `LifetimeCollector`, `UpdateService`, and `ClaudeSetupToken` seams instead
    of exposing raw providers, registries, HTTP clients, subprocesses, cache
    paths, or source callables to commands.
43. Keep `create_app()` registration-only and make help and version execute
    without operational composition or filesystem, credential, scheduler, or
    network initialization.
44. Convert the CLI module, help adapter, and token input to their final
    package paths atomically; verify source, sdist, and an isolated exact-wheel
    install contain every command owner and no stale flat modules.
45. Test the CLI conversion with the fewest behavior-bearing lifecycle,
    blocked-state, registration, output, and packaging cases; delete global
    context, existence-smoke, private-helper, and redundant assertion tests
    instead of preserving them through compatibility shims.

## Sign-off — APPROVED

Approved by the operator on 2026-07-09, with the dependency and persistence
decisions and the CS-13 caller-driven model decisions above approved on
2026-07-10.

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
[http-research]: ../research/2026-07-10-http-transport-retry-dependency.md
[stamina-pypi]: https://pypi.org/project/stamina/
[backoff-github]: https://github.com/litl/backoff
[platformdirs-api]: https://platformdirs.readthedocs.io/en/latest/api.html
[platformdirs-platforms]: https://platformdirs.readthedocs.io/en/latest/platforms.html
[platformdirs-pypi]: https://pypi.org/project/platformdirs/
[paths-research]: ../research/2026-07-10-application-path-discovery-dependency.md
[persistence-research]: ../research/2026-07-10-persistence-durability-and-rollback.md
[schema-research]: ../research/2026-07-10-schema-validation-dependency.md
[usage-tui-design]: ./2026-06-19-usage-tui-redesign-design.md
