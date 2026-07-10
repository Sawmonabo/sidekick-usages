# Architecture Enforcement Decision

**Status:** GO
**Date:** 2026-07-10
**Scope:** Maintainable application architecture, CS-22

## Evidence snapshot

- Repository: `Sawmonabo/sidekick-usages`, branch `develop`, pre-CS-22
  evidence commit `56fbd1028b8afe27d4d6dce0d54e2edf06d6553c`.
- Retrieved: 2026-07-10.
- Local baseline: Python 3.14.6 and Ruff 0.15.12 from `uv.lock`.
- Evaluated upstream: Import Linter 2.13, canonical tag `v2.13` at
  `f544debbb0efe10092cd387032ea76b94a0acee0`; Python 3.14 is classified as
  supported, the package requires Python 3.10 or newer, and its license is
  BSD-2-Clause.
- Local scope: `src/`, `tests/`, `packaging/`, `pyproject.toml`,
  `.pre-commit-config.yaml`, the exact wheel verifier, and design section
  16.5.
- External scope: official Ruff rule/settings/versioning documentation,
  Import Linter's canonical repository and contract documentation, and the
  official PyPI release metadata.

Primary sources:

- [Ruff TID251](https://docs.astral.sh/ruff/rules/banned-api/) establishes that
  the built-in rule bans named imports and API accesses.
- [Ruff settings](https://docs.astral.sh/ruff/settings/#lint_flake8-tidy-imports_banned-api)
  defines the supported banned-API configuration.
- [Ruff versioning](https://docs.astral.sh/ruff/versioning/) records that Ruff
  does not yet expose a stable API and uses minor releases for breaking
  changes.
- [Import Linter](https://github.com/seddonym/import-linter) is the canonical
  implementation and documents forbidden, independence, and layered import
  contracts.
- [Import Linter 2.13 metadata](https://pypi.org/project/import-linter/2.13/)
  records the evaluated release and interpreter support.

## Question

Should Sidekick enforce its final dependency and ownership contracts with its
existing tools, adopt an import-boundary dependency, or build a focused
repository checker?

## Ground truth

The implemented architecture has two different kinds of invariant:

1. Ordinary Python quality and import hygiene, including annotations,
   relative imports, unused imports, and banned APIs.
2. Sidekick-specific semantic contracts, including exact operational-context
   fields, the closed doctor and location-state vocabularies, one retry owner,
   one migration coordinator, one robot source, no import-time path discovery,
   and exact source, sdist, and wheel contents.

Ruff already implements `flake8-tidy-imports`, including TID251 banned APIs,
but its documented rule is an import convention rather than an application
architecture graph or AST schema check. See the official
[Ruff TID251 documentation](https://docs.astral.sh/ruff/rules/banned-api/)
and [Ruff settings reference](https://docs.astral.sh/ruff/settings/#lint_flake8-tidy-imports_banned-api).

[Import Linter](https://github.com/seddonym/import-linter) is a focused,
actively developed tool for contracts between Python modules. Its official
documentation describes forbidden, independence, and layered import
contracts. Those are useful when the dominant problem is a package import
graph, but they do not express Sidekick's exact dataclass fields, PEP 695
union members, call ownership, literal enum vocabulary, source-file
conversion, or renderer side-effect rules.

Python's standard `ast` module parses the repository's Python 3.14 grammar and
can express those exact local contracts without a runtime dependency or a
second configuration language. It is not a replacement for Ruff or `ty`; it
is the smallest complement for rules that those tools do not model.

The alternatives were rejected at their actual capability boundary:

- Ruff alone cannot express exact dataclass fields, closed union members,
  source-file conversion, owner counts, or call-site composition rules.
- Import Linter 2.13 is mature and compatible, but its import contracts still
  leave the semantic rules unimplemented and add another dependency plus
  configuration language.
- A general custom architecture framework would exceed the concrete need. A
  focused AST check is acceptable only while it stays repository-specific,
  deterministic, and smaller than adopting plus supplementing another tool.

## Decision

Use the existing tools wherever they are authoritative:

- Ruff owns formatting, annotations, general lint, and import hygiene.
- `ty` owns static type consistency.
- the exact-artifact verifier owns source, sdist, wheel, isolated-install, and
  console-entry-point contracts.
- a focused standard-library AST checker owns only Sidekick-specific semantic
  architecture contracts.

Do not add Import Linter for this migration. It would cover only a subset of
the required checks while retaining the need for a custom semantic checker.
Reconsider it if three or more independently configured packages later need
layered or independence contracts that are awkward to express in the focused
checker.

## Implemented gate

The gate is split by cohesive responsibility:

- `packaging/check_architecture.py` coordinates dependency, context, time,
  schema, CLI, and source-shape checks;
- `packaging/architecture_ast.py` owns typed parsing and diagnostic
  primitives; and
- `packaging/architecture_ownership.py` owns the four single-owner rules for
  paths, migrations, HTTP retry, and branding.

The pre-commit gate runs the checker after relevant source, test, packaging,
or project-metadata changes. One concise negative test snapshot introduces a
deliberate violation for every advertised failure-rule family and requires the
complete rule-id set. A separate threshold test proves that 800-line cohesion
reviews warn while the 1,000-line limit fails closed.

## Reversal criteria

Reopen the decision if the checker starts reimplementing a maintained tool,
requires broad parsing beyond Python ASTs, grows a plugin framework, or cannot
state a rule in one cohesive owner without exception lists that obscure the
architecture. In that case, compare the current versions and official
contracts of candidate tools again before adding a dependency.
