# Schema-validation dependency research

**Change set:** CS-07
**Research date:** 2026-07-10
**Repository branch:** `develop`
**Evidence commit:** `c5b588ad474fd95c597cfd0b64339223e3da1843`
**Python target:** CPython 3.14
**Decision state:** **GO — OPERATOR APPROVED 2026-07-10**
**Approved selection:** **Pydantic 2.13.4 `TypeAdapter` at boundary-local
untrusted-data schemas**

This document is the self-contained, tracked CS-07 research record. It records
the decision question, live repository evidence, exact candidate versions,
measurements, primary sources, recommendation, limitations, mandatory
conditions, and reversal rules. The operator-approved GO authorizes the
selection for its later implementation change. This research-only record does
not itself modify production dependencies or bypass its implementation and
release gates.

## Contents

1. [Decision question](#decision-question)
2. [Executive conclusion](#executive-conclusion)
3. [Live repository evidence](#live-repository-evidence)
4. [Method and corpus](#method-and-corpus)
5. [Validation behavior](#validation-behavior)
6. [Diagnostics and secret safety](#diagnostics-and-secret-safety)
7. [Performance and footprint](#performance-and-footprint)
8. [Packaging and platforms](#packaging-and-platforms)
9. [Licensing, security, and maintenance](#licensing-security-and-maintenance)
10. [Mandatory architecture rules](#mandatory-architecture-rules)
11. [Candidate disposition](#candidate-disposition)
12. [Limitations and uncertainties](#limitations-and-uncertainties)
13. [Reversal conditions](#reversal-conditions)
14. [Testing implications](#testing-implications)
15. [Operator disposition](#operator-disposition)
16. [Primary sources](#primary-sources)

## Decision question

Should Sidekick Usages use focused standard-library parsing, Pydantic
`TypeAdapter`, cattrs, or msgspec for runtime validation of:

- persisted account generations;
- legacy prototype records;
- Claude credential, usage, and refresh payloads;
- Codex auth-file, JWT, usage, and refresh payloads; and
- JSON objects returned by the shared HTTP boundary?

The selected option must:

- reject missing, null, extra, and mistyped data without unsafe coercion;
- produce actionable nested paths, including account labels and list indexes;
- preserve explicit schema generations and fail closed on unknown generations;
- prevent tokens and credential material from entering diagnostics;
- stay at persistence and provider boundaries rather than entering `core/`;
- support Python 3.14 packaging on Linux, macOS, and Windows;
- remain compatible with the repository's generated Homebrew formula; and
- cost less to own than a hand-built validation framework.

This decision does not include configuration-source discovery.
`pydantic-settings` is not approved by this decision.

## Executive conclusion

Pydantic 2.13.4 `TypeAdapter` is the recommended schema engine, with explicit
conditions. It provided the strongest combination of:

- strict configured validation;
- exact structured paths through an account mapping and nested lists;
- stable machine-readable error codes;
- aggregation of independent failures; and
- validation of `TypedDict` and standard-library dataclass shapes without
  requiring core models to inherit from `BaseModel`.

Pydantic's safe behavior is not its default. Its documentation states that it
normally coerces compatible input and ignores extra fields. Boundary schemas
must configure strictness and their extra-field policy explicitly. Raw
validation errors also contain rejected input by default, so no Pydantic error
may cross a boundary or reach a renderer, logger, command, or user-facing
exception chain.

The operator approved this selection on 2026-07-10. Release readiness remains
conditional because Pydantic adds a five-distribution, 7.28 MB installed
closure and `pydantic-core` is built with Rust and maturin. The current
Sidekick Homebrew generator prefers source distributions but does not declare
those build tools. A clean supported-platform Homebrew build is a mandatory
release gate, not follow-up polish.

## Live repository evidence

The evidence commit contains these untrusted-data boundaries:

- `src/sidekick_usages/store.py` stores an unversioned JSON object keyed by
  account label. Its current record has provider, access, refresh, expiry,
  scope, Codex-auth, refresh-status, and heartbeat fields.
- `Account.from_dict()` accepts both the current record and the prototype
  `{ "token": ..., "plan": ... }` shape. It defaults missing values and calls
  `bool(...)` for `heartbeat_enabled`; therefore the corrupt JSON string
  `"false"` becomes `True`.
- `src/sidekick_usages/providers/claude.py` reads local `claudeAiOauth`
  credentials, optional usage windows, and OAuth refresh responses.
- `src/sidekick_usages/providers/codex.py` reads Codex `auth.json`, nested JWT
  claims, primary and additional rate-limit windows, and refresh responses.
- `src/sidekick_usages/http.py` decodes JSON and uses unchecked dictionary
  casts. A syntactically valid list, scalar, or `null` can therefore reach code
  that claims to have a JSON object.
- `packaging/homebrew/generate.py` resolves every runtime distribution,
  selects its source distribution when available, and emits a Homebrew
  resource. The generated formula currently declares only Python 3.14 as a
  build/runtime dependency.
- `.github/workflows/ci.yml` runs Python 3.14 on Linux, macOS, and Windows.

These are eight distinct schema families rather than one global schema:

1. current persisted accounts;
2. prototype persisted accounts;
3. Claude credentials;
4. Claude usage responses;
5. Codex auth files;
6. Codex JWT claims;
7. Codex usage responses; and
8. OAuth refresh responses.

Stored schemas and provider schemas need different compatibility policies.
Persisted generations are closed and versioned. A provider boundary may allow
documented forward-compatible fields, but that choice must be explicit in the
provider's schema rather than inherited from a global default.

## Method and corpus

The comparison used four fresh CPython 3.14.6 environments:

- Python standard-library `json` plus focused validators;
- Pydantic 2.13.4 with pydantic-core 2.46.4;
- cattrs 26.1.0 with attrs 26.1.0; and
- msgspec 0.21.1.

The machine was Ubuntu 22.04.5 under WSL2, Linux 6.6.87.2, glibc 2.35,
x86-64, with six exposed logical CPUs. uv 0.11.21 created and populated the
isolated environments. No real credential, provider request, production
installation, or account file participated.

The synthetic valid corpus covered all eight schema families. The current
store contained one Claude and one Codex account. The Codex usage payload
included an additional named rate-limit bucket.

The current-store mutation corpus was:

1. missing required `access_token`;
2. an unexpected account field;
3. null required `access_token`;
4. a string in optional integer `expires_at`;
5. string `"false"` in boolean `heartbeat_enabled`;
6. an integer at `scopes[1]`;
7. an array instead of the top-level object; and
8. a unique secret sentinel embedded in the rejected token value.

Strict candidate configuration was deliberate:

- the standard-library prototype owned exact key and type checks;
- Pydantic used strict validation and forbidden extras;
- cattrs used detailed validation, forbidden extras, and exact scalar hooks;
- msgspec used strict decoding and `forbid_unknown_fields=True` on every
  `Struct`.

Results below describe those safe configurations. They do not imply that the
libraries' defaults are equivalent.

## Validation behavior

All four configured candidates accepted all eight valid schema families and
rejected all eight mutations.

| Criterion | Standard library | Pydantic | cattrs | msgspec |
|---|---|---|---|---|
| Strict scalar default | Must implement | No; configure | No; hooks required | Yes |
| Extra-field default | Must implement | Ignore | Ignore | Ignore |
| Exact account-label path | Manual | Yes, structured | Yes, formatted | No; `$[...]` |
| Nested list index | Manual | Yes | Yes | Yes |
| Multiple independent errors | First in prototype | Yes | Yes | First |
| Machine-readable error code | Owned code required | Yes | No stable equivalent used | No structured code used |
| Boundary type coupling | Owned framework | Low with `TypedDict` | Low with dataclasses | Higher with `Struct` |

For two simultaneous corruptions, Pydantic returned both:

```text
('long.account.name@example.test', 'access_token') -> missing
('codex-pro', 'heartbeat_enabled') -> bool_type
```

cattrs also aggregated both failures and preserved both account labels.
The standard-library prototype and msgspec reported the first failure only.
msgspec rendered a record beneath `dict[str, Struct]` as `$[...]`, omitting the
label needed to repair a multi-account store.

For an integer at the second `scopes` element:

- Pydantic returned
  `('long.account.name@example.test', 'scopes', 1)`;
- cattrs rendered
  `$['long.account.name@example.test'].scopes[1]`; and
- msgspec rendered `$[...].scopes[1]`.

cattrs required application-owned exact hooks for `bool`, `float`, `int`, and
`str`. Its optional integer failure rendered `expected Union`, which was less
actionable than Pydantic's `int_type` code. Those hooks and diagnostic
customization are cohesive but increase Sidekick-owned validation policy.

The standard-library version was strict and secret-safe, but only because it
implemented recursive object checks, required and allowed key sets, scalar and
collection checks, union handling, path formatting, and an error vocabulary.
Repeating that work for every boundary would create the custom framework this
decision is intended to avoid.

## Diagnostics and secret safety

Pydantic's documented `ErrorDetails` contains `input`, `loc`, `msg`, `type`,
and `url`. `hide_input_in_errors` defaults to false. The comparison confirmed
that an invalid secret appears in default `str(ValidationError)` and
`errors()` output.

The secret sentinel was absent after projecting:

```python
error.errors(include_input=False, include_url=False)
```

into Sidekick-owned data containing only:

```text
path
code
safe message
```

This projection is mandatory. `hide_input_in_errors=True` is defense in depth,
not the application error contract. The boundary must also discard Pydantic
`ctx` values because custom contexts can contain unsafe objects or messages.

No public or logged error may retain the raw validation exception as a
user-visible cause. Redaction tests must cover access tokens, refresh tokens,
ID tokens, authorization headers, payload fragments, and full account
identities with distinct sentinels.

The standard-library, cattrs `transform_error`, and msgspec renderings did not
expose the sentinel in this corpus. That does not remove the requirement for an
application-owned error contract if either alternative is selected later.

## Performance and footprint

Decode timing included JSON decoding and validation of the synthetic
two-account current store. Each candidate ran seven repeats of 20,000
operations.

| Candidate | Median us/op | Minimum | Maximum |
|---|---:|---:|---:|
| Standard library | 7.820 | 7.595 | 9.893 |
| Pydantic | 4.449 | 4.268 | 4.795 |
| cattrs | 13.194 | 12.635 | 15.908 |
| msgspec | 1.145 | 1.113 | 1.245 |

Fresh-process import measurements used 80 processes.

| Environment | Median ms | p95 ms |
|---|---:|---:|
| Python process baseline | 11.11 | 13.54 |
| Pydantic import | 36.16 | 43.94 |
| cattrs import | 41.16 | 84.57 |
| msgspec import | 27.27 | 40.31 |

Cold process, import, all schema definitions, and validation of all eight
valid families used 60 processes.

| Candidate | Median ms | p95 ms |
|---|---:|---:|
| Standard library | 44.81 | 56.46 |
| Pydantic | 103.33 | 132.81 |
| cattrs | 72.06 | 80.01 |
| msgspec | 48.01 | 59.06 |

Clean installed closures excluded `__pycache__`.

| Candidate | Runtime distributions | Installed bytes | Linux wheel bytes |
|---|---|---:|---:|
| Standard library | none | 0 marginal | 0 |
| Pydantic | 5 | 7,281,761 | 2,645,819 |
| cattrs | 3 | 677,508 | 186,173 |
| msgspec | 1 | 532,136 | 224,993 |

The Pydantic distributions were `pydantic`, `pydantic-core`,
`annotated-types`, `typing-extensions`, and `typing-inspection`. The cattrs
distributions were `cattrs`, `attrs`, and `typing-extensions`.

At Sidekick's account volume, all decode results are fast enough. Diagnostic
quality, startup on real CLI paths, and packaging are more important than the
microsecond ranking. Later implementation must measure `--help`, `--version`,
the default dashboard, and `doctor`; help and version must not initialize
operational resources.

## Packaging and platforms

Pydantic itself publishes a universal Python wheel, while pydantic-core
publishes native CPython 3.14 wheels. At the research date, pydantic-core
2.46.4 provided relevant wheels for:

- Linux x86-64 and AArch64;
- macOS x86-64 and ARM64; and
- Windows AMD64.

msgspec also published CPython 3.14 native wheels for the main Sidekick CI
platforms. cattrs and its runtime closure used universal pure-Python wheels.
Wheel metadata is installation evidence, not cross-platform runtime proof.
Only Linux executed the comparison.

### Mandatory Homebrew gate

The current Homebrew generator prefers a source distribution for every runtime
package. Pydantic-core 2.46.4 declares:

- maturin `>=1.10,<2` as its PEP 517 build backend; and
- Rust 1.88 as its minimum Rust version.

The official Homebrew Pydantic 2.13.4 formula likewise declares maturin and
Rust as source-build dependencies. Sidekick's generated formula currently
declares neither.

Pydantic cannot enter Sidekick's production dependencies until a later
implementation change proves all of the following:

1. the generated formula declares every required build input under the
   repository's source-distribution policy;
2. a clean Homebrew build does not fetch undeclared build dependencies;
3. formula generation remains deterministic and resource-complete;
4. the formula installs and imports Sidekick on supported macOS and Linux
   targets;
5. the built formula passes its command smoke tests; and
6. the added Rust/maturin build and maintenance cost is accepted by the
   operator.

Changing the generator to select platform wheels would be a separate packaging
design with its own platform-resource and reproducibility proof. Wheel
availability alone is not permission to bypass the current source policy.

## Licensing, security, and maintenance

Snapshot data from canonical repositories and PyPI on 2026-07-10:

| Project | Exact release | Classifier | License | Archived | Last push observed |
|---|---|---|---|---:|---|
| Pydantic | 2.13.4, 2026-05-06 | Production/Stable | MIT | No | 2026-07-06 |
| cattrs | 26.1.0, 2026-02-18 | Production/Stable | MIT | No | 2026-07-09 |
| msgspec | 0.21.1, 2026-04-12 | Beta | BSD-3-Clause | No | 2026-07-09 |

All three declared Python 3.14 support. Repository snapshots showed active,
non-archived projects with recent releases and pushes. Popularity was observed
but was not used as an approval criterion.

Exact-version queries to both PyPI's vulnerability field and the OSV API
returned no matching advisories for:

- Pydantic 2.13.4;
- pydantic-core 2.46.4;
- cattrs 26.1.0;
- attrs 26.1.0; and
- msgspec 0.21.1.

This means only that those sources returned no known match on the research
date. It is not proof that a package has no vulnerability. Advisory, release,
license, provenance, and maintenance checks must be refreshed when the
dependency is added or upgraded.

## Mandatory architecture rules

The approved implementation must obey these rules.

### Boundary ownership

- Persisted generations live in `persistence/schemas.py`.
- Claude schemas live in `providers/claude/schemas.py`.
- Codex schemas live in `providers/codex/schemas.py`.
- The HTTP client proves only the transport-level JSON-object contract.
  Provider-specific field validation remains provider-owned.
- `core/models.py`, `core/types.py`, core policy, services, renderers, and
  commands do not import Pydantic or expose a Pydantic type.
- Boundary functions return plain validated mappings or convert immediately to
  Sidekick-owned dataclasses and closed types.
- Business invariants remain in core or the owning feature. Pydantic validators
  do not become a hidden business-policy layer.

### Strict configuration

- Cache each `TypeAdapter` at module scope.
- Use `@with_config(ConfigDict(...))` for boundary `TypedDict` or standard
  dataclass schemas.
- Enable strict behavior explicitly.
- Set `hide_input_in_errors=True` as defense in depth.
- Give each boundary an explicit extra-field policy.
- Forbid extras in durable stored generations.
- Do not globally forbid provider additions without deciding that provider's
  forward-compatibility contract.
- Do not use coercive defaults to turn corruption into apparently valid state.

### Error contract

- Catch `ValidationError` inside the boundary that owns the schema.
- Project only the structured path, stable code, and safe message.
- Exclude `input`, `url`, `ctx`, raw exception text, and unsafe exception
  causes.
- Raise the existing explicit application error vocabulary or one deliberately
  designed boundary error the caller must handle.
- Fail closed; never convert malformed persisted or credential data into an
  empty/default record.

### Stored generations

- Dispatch the unversioned current store as explicit generation zero.
- Give every supported generation its own strict schema.
- Migrate a validated old generation through a pure, explicit conversion.
- Reject unknown future generations.
- Never use an ambiguous union whose successful branch silently changes when a
  field is added.
- Validate the complete document before any authoritative rewrite.

### Dependency containment

- No `BaseModel` inheritance in core or public product models.
- No raw Pydantic result or exception crosses a persistence/provider boundary.
- No library-specific assertion appears in command or rendering tests.
- Do not add `pydantic-settings`; it solves a different configuration-source
  problem and has no approved contract here.

These rules keep reversal feasible. A future engine change should affect
boundary schema modules and their private error translation, not core types or
callers.

## Candidate disposition

| Candidate | Research disposition | Reason |
|---|---|---|
| Pydantic 2.13.4 `TypeAdapter` | **GO — operator approved** | Best structured diagnostics and strict boundary fit; release packaging gate remains |
| cattrs 26.1.0 | Leading alternative, not approved | Smaller pure-Python closure, but strict hooks and weaker optional diagnostics add owned policy |
| msgspec 0.21.1 | NO-GO for this use case | Speed is unnecessary; mapping-key path and first-error behavior reduce repairability |
| Focused standard-library framework | NO-GO unless all mature options fail | Reimplements nested validation, aggregation, paths, unions, and errors |

The approved selection is Pydantic because actionable repair and safe typed
failures are more valuable than microsecond decode speed. Release remains
blocked until the Homebrew gate succeeds.

If the Pydantic gate fails, this record does not automatically authorize
cattrs. Reopen CS-07 with cattrs as the first alternative, refresh its evidence,
and obtain an operator disposition. Do not weaken validation or improvise a
custom framework.

## Limitations and uncertainties

- The executable comparison ran only on Linux CPython 3.14.6.
- Published wheels and classifiers do not prove macOS or Windows runtime
  behavior.
- Sidekick's own Homebrew formula was not rebuilt with Pydantic.
- The corpus was synthetic and source-derived. It deliberately used no real
  account or provider data.
- Provider payloads can evolve. Implementation needs sanitized fixtures for
  every currently supported alternate shape, including reset-key variants.
- Strict validation of JSON-native primitives was measured. Pydantic documents
  different strict JSON behavior for richer types such as dates and UUIDs;
  future additions need explicit tests.
- Startup figures are single-machine measurements influenced by filesystem
  cache and scheduling. They are not end-to-end CLI latency measurements.
- The Homebrew build-tool requirement is proven from build metadata and the
  official formula, but the exact Sidekick generator change remains to be
  designed and tested.
- Dependency state and advisories can change after the research date.

## Reversal conditions

Reopen CS-07 if any of the following becomes true:

- a deterministic supported-platform Homebrew build cannot be maintained;
- the Rust/maturin source-build cost is rejected by the operator;
- Pydantic exceptions, inputs, contexts, or URLs escape a boundary;
- core or public product models begin importing or inheriting from Pydantic;
- strict validation requires pervasive custom validators that recreate
  business logic at the edge;
- a Pydantic major or minor upgrade changes accepted inputs, extra-field
  handling, error paths, or redaction without a compatible tested migration;
- measured real CLI startup becomes materially unacceptable;
- a material security, provenance, licensing, or maintenance problem lacks a
  timely supported remediation; or
- provider or persistence requirements change so that another candidate
  deletes substantial owned code while retaining diagnostic quality.

At reversal, preserve the boundary function signatures, Sidekick-owned output
types, and application error contract. Evaluate cattrs first, but require a new
recorded decision rather than an automatic dependency swap.

## Testing implications

No production or behavior test is warranted for this research-only Markdown
record. The appropriate verification for this change is repository Markdown
lint and a clean diff check.

Implementation tests must be concise and load-bearing:

1. One table-driven boundary contract test covers missing, extra, null,
   mistyped scalar, coercion trap, nested list item, wrong root, and multiple
   failures.
2. One redaction test places distinct sentinels in access, refresh, and ID-token
   positions and checks every public error/rendering surface.
3. One stored-generation test proves generation-zero migration, current
   round-trip, unknown-generation rejection, and no partial rewrite after a
   failure.
4. Focused provider tests cover only materially different provider
   compatibility rules.
5. One packaging acceptance path proves the clean Homebrew source build, while
   the existing CI matrix exercises Linux, macOS, and Windows.

Do not assert complete Pydantic error strings, private library calls, or a
coverage number. Assert Sidekick's stable path/code/message contract and
observable behavior. Do not add copy-paste tests for every field when one clear
parameterized case makes the same regression fail.

## CS-17A implementation follow-up

The provider-package implementation retained boundary-local Pydantic adapters,
strict scalar validation, explicit extra-field policy, and Sidekick-owned
error projection. Provider failures now distinguish missing, unreadable,
malformed, incomplete, expired, rejected, identity-mismatched, and unsupported
state without retaining rejected values or validation exceptions.

Native source failures also require the same closed classification. Apple's
Security documentation distinguishes an absent keychain item
(`errSecItemNotFound`, OSStatus `-25300`) from an inaccessible locked item
(`errSecInteractionNotAllowed`, OSStatus `-25308`). The macOS Claude adapter
therefore treats only the shell exit corresponding to `-25300` as `MISSING`;
other nonzero `security find-generic-password` exits are `UNREADABLE`. Mapping
every nonzero exit to absence would conceal a real access failure and could
incorrectly prompt for replacement credentials.

The saved-account refresh subprocess requires a stronger isolation boundary
than changing `HOME`. Anthropic documents that Linux and Windows credentials
move under `CLAUDE_CONFIG_DIR` when it is set, while macOS credentials remain
in the system Keychain. Anthropic also documents `%USERPROFILE%\.claude` as the
default Windows credential location. A caller-provided `CLAUDE_CONFIG_DIR` or
unchanged Windows profile can therefore defeat a temporary `HOME`; on macOS,
no documented per-invocation Keychain namespace exists.

The implementation consequence is fail-closed and platform-specific: saved
CLI refresh is not attempted on macOS, and supported file-backed platforms
must replace every relevant config/profile variable with an invocation-owned
directory instead of inheriting user state. Provider subprocess output is an
untrusted secret-bearing boundary, so Sidekick returns structured outcomes
with Sidekick-owned messages rather than forwarding output after a token
pattern blacklist. Unused refresh output is discarded and setup-token capture
is bounded.

## Operator disposition

| Field | Value |
|---|---|
| Change set | CS-07 |
| Research recommendation | GO for Pydantic 2.13.4 `TypeAdapter` |
| Current state | **GO — OPERATOR APPROVED** |
| Production addition | Authorized for the later implementation change after its gates |
| Selected option | Boundary-local Pydantic 2.13.4 `TypeAdapter` |
| Operator decision | GO |
| Approval date | 2026-07-10 |
| Homebrew release gate | Rust and maturin source-build proof required |
| Design commit containing approval | Pending tracked design update |
| Approved design SHA-256 | Pending tracked design update |

The operator approved Pydantic with the Rust/maturin Homebrew proof retained as
a mandatory release gate. The design authority and plan ledger must record the
approval commit and approved-content SHA-256 before a dependent implementation
change relies on them.

## Primary sources

All external observations below were retrieved on 2026-07-10 unless the link is
an immutable release or tagged source.

### Python and repository boundaries

- [Python 3.14 `json` documentation][python-json]
- Evidence commit source paths:
  `src/sidekick_usages/store.py`,
  `src/sidekick_usages/providers/claude.py`,
  `src/sidekick_usages/providers/codex.py`,
  `src/sidekick_usages/http.py`,
  `packaging/homebrew/generate.py`, and
  `.github/workflows/ci.yml`

### Pydantic

- [TypeAdapter documentation][pydantic-adapter]
- [Strict mode][pydantic-strict]
- [Error handling and structured details][pydantic-errors]
- [Configuration, extra fields, input hiding, and `with_config`][pydantic-config]
- [Pydantic 2.13.4 package metadata][pydantic-json]
- [pydantic-core 2.46.4 package metadata][pydantic-core-json]
- [Tagged pydantic-core PEP 517 build metadata][pydantic-core-build]
- [Tagged pydantic-core Cargo manifest][pydantic-core-cargo]
- [Pydantic 2.13.4 release][pydantic-release]
- [Pydantic canonical repository][pydantic-repo]
- [Pydantic MIT license][pydantic-license]

### cattrs

- [cattrs 26.1.0 documentation][cattrs-docs]
- [Detailed validation and error transformation][cattrs-validation]
- [Extra-key configuration][cattrs-extra]
- [Migration notes][cattrs-migrations]
- [cattrs 26.1.0 package metadata][cattrs-json]
- [cattrs canonical repository][cattrs-repo]
- [cattrs MIT license][cattrs-license]

### msgspec

- [msgspec documentation][msgspec-docs]
- [Strict typed decoding][msgspec-strict]
- [Unknown-field behavior][msgspec-fields]
- [Schema evolution guidance][msgspec-evolution]
- [msgspec 0.21.1 package metadata][msgspec-json]
- [msgspec canonical repository][msgspec-repo]
- [msgspec BSD-3-Clause license][msgspec-license]

### Packaging and advisory sources

- [Homebrew Python formula guidance][homebrew-python]
- [Official Homebrew Pydantic formula][homebrew-pydantic]
- [Official Homebrew Pydantic formula source][homebrew-pydantic-source]
- [OSV API documentation][osv-api]

### Provider source classification

- [Apple `errSecItemNotFound` documentation][apple-item-not-found]
- [Apple Security framework keychain pitfalls][apple-keychain-pitfalls]
- [Anthropic credential management][anthropic-credentials]
- [Anthropic Claude configuration-directory behavior][anthropic-directory]
- [Anthropic environment-variable reference][anthropic-environment]

[python-json]: https://docs.python.org/3.14/library/json.html
[pydantic-adapter]: https://pydantic.dev/docs/validation/latest/concepts/type_adapter/
[pydantic-strict]: https://pydantic.dev/docs/validation/latest/concepts/strict_mode/
[pydantic-errors]: https://pydantic.dev/docs/validation/latest/errors/errors/
[pydantic-config]: https://pydantic.dev/docs/validation/latest/api/pydantic/config/
[pydantic-json]: https://pypi.org/pypi/pydantic/2.13.4/json
[pydantic-core-json]: https://pypi.org/pypi/pydantic-core/2.46.4/json
[pydantic-core-build]: https://raw.githubusercontent.com/pydantic/pydantic/v2.13.4/pydantic-core/pyproject.toml
[pydantic-core-cargo]: https://raw.githubusercontent.com/pydantic/pydantic/v2.13.4/pydantic-core/Cargo.toml
[pydantic-release]: https://github.com/pydantic/pydantic/releases/tag/v2.13.4
[pydantic-repo]: https://github.com/pydantic/pydantic
[pydantic-license]: https://github.com/pydantic/pydantic/blob/main/LICENSE
[cattrs-docs]: https://catt.rs/en/stable/
[cattrs-validation]: https://catt.rs/en/stable/validation.html
[cattrs-extra]: https://catt.rs/en/stable/customizing.html#forbid-extra-keys
[cattrs-migrations]: https://catt.rs/en/stable/migrations.html
[cattrs-json]: https://pypi.org/pypi/cattrs/26.1.0/json
[cattrs-repo]: https://github.com/python-attrs/cattrs
[cattrs-license]: https://github.com/python-attrs/cattrs/blob/main/LICENSE
[msgspec-docs]: https://jcristharif.com/msgspec/
[msgspec-strict]: https://jcristharif.com/msgspec/usage.html#strict-vs-lax-mode
[msgspec-fields]: https://jcristharif.com/msgspec/structs.html#forbidding-unknown-fields
[msgspec-evolution]: https://jcristharif.com/msgspec/schema-evolution.html
[msgspec-json]: https://pypi.org/pypi/msgspec/0.21.1/json
[msgspec-repo]: https://github.com/msgspec/msgspec
[msgspec-license]: https://github.com/msgspec/msgspec/blob/main/LICENSE
[homebrew-python]: https://docs.brew.sh/Python-for-Formula-Authors
[homebrew-pydantic]: https://formulae.brew.sh/formula/pydantic
[homebrew-pydantic-source]: https://raw.githubusercontent.com/Homebrew/homebrew-core/HEAD/Formula/p/pydantic.rb
[osv-api]: https://google.github.io/osv.dev/api/
[apple-item-not-found]: https://developer.apple.com/documentation/security/errsecitemnotfound
[apple-keychain-pitfalls]: https://developer.apple.com/forums/thread/724013
[anthropic-credentials]: https://code.claude.com/docs/en/team
[anthropic-directory]: https://code.claude.com/docs/en/claude-directory
[anthropic-environment]: https://code.claude.com/docs/en/env-vars
