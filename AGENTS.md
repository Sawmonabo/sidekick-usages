# Repository Guidelines

## Project Structure and Ownership

This Python 3.14 package uses a `src/` layout. Keep behavior with its owning
boundary:

- `src/sidekick_usages/cli/` owns the registration-only Typer root, typed lazy
  composition, help adapter, token input, and cohesive command modules.
- `core/` owns infrastructure-free models, identifiers, expiry rules, and UTC
  invariants. It must not depend on CLI, HTTP, filesystem, settings, or
  operating-system path discovery.
- `providers/claude/` and `providers/codex/` own provider schemas, credential
  detection, refresh behavior, usage and token-activity calls, and heartbeat
  adapters.
- `credentials/` owns provider-neutral credential workflows, Claude
  transition/lifetime/restore policy, serialized refresh coordination, and
  private Codex bundle coordination; `http/` owns pooled HTTPS transport and
  retry policy.
- `persistence/` owns strict schemas, qualified filesystem operations,
  account/private and refresh transactions, recovery, and provider-neutral
  migrations; `serialization/` owns strict JSON decoding.
- `usage/activity.py` owns scoped token-activity collection and aggregation;
  the rest of `usage/` owns usage results and Rich presentation. `heartbeat/`,
  `daemon.py`, and `maintenance.py` own scheduled maintenance.
- `branding.py` is the canonical robot and product-copy source, `paths.py` is
  the sole Sidekick application-path owner, and `clock.py` owns wall-clock
  acquisition.

Tests in `tests/` generally mirror package owners. Keep operational docs in
`docs/`, quality and artifact verification in `packaging/`, Homebrew packaging
in `packaging/homebrew/`, and automation in `.github/workflows/`. Do not commit
generated distributions, caches, coverage, virtual environments, or credential
files.

## Setup, Test, and Quality Commands

- `uv sync --all-groups`: create the project environment and install the
  locked project plus development, lint, and test groups. The project is
  editable by default; do not follow it with `uv pip install -e .`.
- `uv run sidekick-usages -h`: exercise the working-tree CLI. `-h` and
  `--help` work at every command level.
- `uv run pytest tests/<owner>/test_<behavior>.py`: iterate on the smallest
  relevant behavior suite.
- `uv run pytest --cov=sidekick_usages`: run the full branch-coverage suite.
- `uv run ruff check src/ tests/ packaging/` and
  `uv run ty check src/ tests/ packaging/`: lint and type-check production,
  test, and packaging code.
- `uv run python packaging/check_architecture.py`: enforce repository-specific
  ownership, dependency, path, context, clock, type, and package contracts.
- `uv run pre-commit run --all-files`: run the complete local static gate.
- `npm ci`, `npm audit --audit-level=moderate`, and
  `npm run lint:markdown`: reproduce the Node.js 22 documentation gate.
- `uv build`: create the wheel and source distribution in `dist/`.
- `uv run python packaging/smoke_wheel.py --build`: build, inspect, install,
  and exercise one exact wheel outside the checkout.

Run focused checks first and the full relevant gates before handoff. CI also
tests Python 3.14 on Linux, macOS Arm and Intel, and Windows, builds the
Homebrew source path on Linux and macOS, and validates the exact
distributions.

## Investigation, Reuse, and Abstraction

Before adding a constant, helper, map, type, service, or dependency, search the
owning package for the exact concept name with `rg`. Read two or three
neighboring files and match their naming, structure, comment density, and error
vocabulary. A second implementation of the same concept is a defect.

Apply the rule of three before extracting shared machinery. Prefer small,
domain-focused classes and functions, keep abstractions private until multiple
modules need them, and do not add speculative parameters, hooks, flags, or
extension points. Choose complete product behavior and maintainable boundaries,
not merely the smallest diff; every mechanism must serve concrete behavior.
For consequential infrastructure, compare a maintained dependency with a local
implementation and record the build-versus-adopt decision in tracked docs.

## Style, Types, and Module Hygiene

Use four-space indentation, double quotes, LF endings, and the 79-column Ruff
format. Use `snake_case` for modules and functions, `PascalCase` for classes,
and `UPPER_SNAKE_CASE` for constants. Public functions and methods require
explicit parameter and return types. Make optional state explicit and prefer
illegal states to be unrepresentable.

Do not introduce `Any`, unjustified `cast(...)`, or an equivalent type escape.
Prefer Python 3.14 PEP 695 aliases and generics plus standard-library types
such as `Path`, aware `datetime`, `StrEnum`, `IntEnum`, `HTTPMethod`, and
`HTTPStatus`. Use native deferred annotations; do not add the deprecated
stringizing `from __future__ import annotations` behavior.

Write concise Sphinx-style docstrings that explain what a unit does. Use
`:param <name>:`, `:returns:`, and `:raises <Exception>:` only when they add
information; omit `:returns:` for `-> None`. Keep code, comment, and docstring
lines within 79 characters.

The hard module limit is 1000 lines; approximately 800 lines requires a
cohesion review. Leave no dead code, unused imports, commented-out blocks, or
stale comments. Do not add blanket or unjustified `# noqa`, `# type: ignore`,
`# nosec`, or type-cast suppressions. An unavoidable rule-specific suppression
requires explicit architecture approval and a one-line justification.

## Error Handling and Boundaries

Do not swallow failures or replace a real error with a plausible default.
Distinguish missing, malformed, unreadable, expired, rejected, unsupported, and
transient states. Fail closed when credentials or persisted state cannot be
trusted. Preserve the existing typed error vocabulary; services and adapters
return or raise typed states, while CLI command owners render them.

Validate untrusted HTTP, JSON, provider, subprocess, and persistence data at
the boundary. Keep provider-specific behavior in provider adapters, retry
policy in `http/`, application paths in `paths.py`, and runtime validation in
the owning schema or boundary model.

## Testing Guidelines

Every test must be able to fail for a meaningful behavioral reason. Keep the
fewest load-bearing tests that satisfy the acceptance criteria; delete
redundant, inert, brittle, implementation-coupled, or coverage-padding cases.
Prefer public command, service, transaction, and adapter boundaries over
private-helper assertions. Use exact output assertions only for intentional
product contracts.

Inject typed fakes at provider, HTTP, filesystem, clock, subprocess, and
scheduler boundaries. Never require real credentials, mutate provider logins,
or depend on public network access. Test code follows the same type and
maintenance standards as production code. Pytest uses strict configuration,
discovers `test_*.py` files beneath `tests/` and `test_*` functions, and has
no minimum coverage threshold.

## Commit and Pull Request Guidelines

Use Conventional Commits such as `feat(render): ...`, `fix(cli): ...`,
`test: ...`, `docs: ...`, `chore: ...`, or `ci: ...`. Install the configured
pre-commit and commit-message hooks. Do not commit directly to `main` and do
not push unless explicitly requested.

Pull requests should explain behavior and impact, list verification commands,
link issues, and update relevant docs and tests. Include before/after terminal
captures for CLI or TUI changes.

## Security and Local State

Never commit access, refresh, or ID tokens; account exports; private Codex
bundles; provider credential files; or files from Sidekick's native or
compatibility application-data locations. Obsolete cache artifacts are also
local state, never fixtures. Use synthetic account and provider identities in
logs and fixtures, and keep credential fields out of representations and
diagnostics.

Require HTTPS and verified TLS for provider traffic. Preserve bounded response,
timeout, retry, and server-wait policies, and never retry a credential-bearing
mutation without an explicit operation-safety basis. Redact provider failures
before persistence or display.

Saved-account maintenance must never adopt, modify, or overwrite the user's
active Claude or Codex login. Preserve private file permissions, atomic-write
and recovery invariants, and the separation between durable credential state
and read-only provider activity state. Use the CLI for migrations and
credential changes; do not manually copy or edit account or private-auth
state.
