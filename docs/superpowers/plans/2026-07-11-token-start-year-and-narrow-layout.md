# Token Start Year and Narrow Layout Implementation Plan

> **For agentic workers:** Preserve provider accounting and persistence. This
> change owns presentation terminology, responsive structure, verification,
> and publication only.

- **Status:** Implemented and verified
- **Date:** 2026-07-11
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Baseline:** `762be0e48aba5a37eb59e9cf4b24f764110473c5`
- **Upstream:** `origin/develop`

## 1. Outcome

Both provider panels render the full verified token-start year:

```text
917,529,698 tokens  ·  since Dec 28, 2025
7,486,342,730 tokens  ·  since Apr 7, 2026
```

The supported narrow layout renders deliberate lines instead of relying on
incidental terminal wrapping:

```text
CLAUDE · 917.53M tokens
         since Dec 28, 2025

CODEX · 7.486B tokens
        since Apr 7, 2026
```

Active code and current documentation call this responsive presentation the
`narrow` layout, not the `legacy` layout. Genuine persistence compatibility,
historical-plan, Python, and Rich API uses of `legacy` remain unchanged.

## 2. Ground truth and scope

Claude's provider-owned `firstSessionDate` contains a complete timestamp.
Codex's reconciled daily activity buckets contain ISO `startDate` values.
Sidekick already normalizes both to `datetime.date`, and the Codex activity
snapshot stores the complete ISO date. Only the shared formatter currently
drops the year.

The panel-fit decision remains content-driven:

```python
required = max(
    FULL_HEADER_MIN_WIDTH,
    *(_panel_min_width(measure, panel) for panel in panels),
)
```

Adding the year does not change provider acquisition, totals, aggregation,
snapshot authority, authentication behavior, or the fit algorithm. No schema
migration or dependency is justified.

The independently verified values at planning time were:

- Claude: `917,529,698`, since `2025-12-28`;
- Codex available account profile: `7,486,342,730`, since `2026-04-07`;
- arithmetic verified subtotal: `8,403,872,428`; and
- one additional saved Codex account remained authentication-rejected without
  an authoritative snapshot and therefore was not fabricated into the sum.

The combined subtotal is audit evidence, not a new product footer.

## 3. Design

### 3.1 Explicit presentation functions

Replace the Boolean-mode `activity_text(..., compact=...)` interface with:

```python
def panel_activity_text(activity: ProviderTokenActivity) -> Text: ...

def narrow_activity_lines(
    activity: ProviderTokenActivity,
) -> tuple[Text, ...]: ...
```

The panel owner always renders exact one-line output. The narrow owner always
renders compact, deliberately structured output. This makes incompatible mode
combinations unrepresentable without speculative options.

The shared private formatter returns `Mon D, YYYY`:

```python
def _format_since(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"
```

### 3.2 Narrow structure

The narrow overview prefixes the first activity line with the provider name.
Every continuation line is indented by the exact provider-prefix cell width.
A summary with no authoritative date remains one line. Unavailable and failed
states retain their existing typed copy. Account recovery warnings remain
separate blocks.

### 3.3 Ownership rename

Rename:

```text
usage/legacy_render.py       -> usage/narrow_render.py
_legacy_activity_blocks      -> _narrow_activity_blocks
_legacy_overview             -> _narrow_overview
```

Update renderer imports, architecture ownership, packaging smoke checks, and
packaging tests. Do not retain an unused compatibility alias for an internal
module.

### 3.4 Documentation authority

Update the README, current TUI design, current architecture design, and the
completed durable-activity plan's presentation examples and supersession
pointer. Do not mass-edit historical research or completed plans whose use of
`legacy` describes historical evidence.

## 4. Meaningful tests

Update the existing load-bearing rendering tests rather than adding padding:

1. The parameterized 120/40-column contract test covers both providers, exact
   and compact totals, full years, and deliberate narrow-line alignment.
2. The 40-column warning test proves no line exceeds the configured width and
   authentication recovery remains visible.
3. The 85-column floor test proves full years do not force a healthy framed
   fixture into the narrow layout.
4. The content-fit test is renamed from legacy to narrow while preserving the
   actual fallback behavior.
5. Existing architecture and packaging tests prove the renamed module is
   shipped and owned exactly once.

No private-helper-only test or duplicate provider fixture is warranted.

## 5. File sequence

1. Refactor `usage/activity_render.py` into explicit panel and narrow owners.
2. Rename `usage/legacy_render.py` to `usage/narrow_render.py`.
3. Update `usage/render.py` imports, function names, terminology, and narrow
   line composition.
4. Update `tests/test_render.py` and `tests/test_check_errors.py`.
5. Update architecture and wheel ownership in `packaging/` and packaging
   tests.
6. Update active product and architecture documentation.
7. Run focused and complete gates.
8. Refresh the repository editable install and global uv tool.
9. Exercise both CLI entry points, commit, push, and verify the remote SHA.

## 6. Acceptance criteria

- **AC-01:** Claude framed output shows `since Dec 28, 2025`.
- **AC-02:** Codex framed output shows `since Apr 7, 2026`.
- **AC-03:** Both narrow summaries contain full four-digit years.
- **AC-04:** Narrow token and date lines are deliberate and fit 40 columns.
- **AC-05:** The controlled 85-column fixture still uses framed panels.
- **AC-06:** Responsive selection remains dynamically measured.
- **AC-07:** Active rendering code uses `narrow`, not `legacy`, terminology.
- **AC-08:** The wheel contains `usage/narrow_render.py` and not the removed
  internal module.
- **AC-09:** Provider totals, scopes, failures, snapshots, and authentication
  behavior are unchanged.
- **AC-10:** No dependency, schema migration, or compatibility alias is added.
- **AC-11:** Focused tests, full tests, static gates, documentation lint,
  architecture policy, security checks, and exact-wheel verification pass.
- **AC-12:** The repository and global uv entry points both execute the new
  source successfully.
- **AC-13:** The verified commit is pushed to `origin/develop` and the worktree
  is clean.

## 7. Verification

```bash
uv run pytest tests/test_render.py tests/test_check_errors.py
uv run pytest tests/test_packaging.py tests/test_architecture.py
uv run ruff check src tests packaging
uv run ruff format --check src tests packaging
uv run ty check src tests
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files --show-diff-on-failure
npm run lint:markdown
npm audit --audit-level=moderate
uv run python packaging/check_architecture.py
uv run python packaging/smoke_wheel.py --build
git diff --check
```

Refresh and verify both editable execution paths:

```bash
uv sync --all-groups
uv pip install -e .
uv tool install --force --editable .
COLUMNS=120 NO_COLOR=1 uv run sidekick-usages
COLUMNS=120 NO_COLOR=1 sidekick-usages
```

An authentication-rejected saved account may produce the expected nonzero
manual-action exit after rendering. That is successful runtime verification
when the dashboard has both year-bearing provider footers, contains no
traceback, and preserves its recovery warning.

## 8. Implementation verification record

Implementation and verification completed on `develop` on 2026-07-11.

Behavioral evidence:

- the 120-column framed layout renders both exact totals with four-digit years;
- the 40-column narrow layout renders compact totals and explicitly aligned
  date lines without exceeding the configured width;
- the controlled 85-column fixture remains on the framed-panel path;
- authentication recovery remains visible; and
- active source, architecture, tests, and packaging use `narrow_render.py`
  without retaining an internal compatibility alias.

Complete gates:

- `uv run pytest --cov=sidekick_usages`: 804 passed, four platform-specific
  skips;
- Ruff lint and format checks: passed;
- `uv run ty check src tests`: passed;
- architecture contracts: passed with the same eight pre-existing cohesion
  warnings;
- every pre-commit hook, including Bandit and vulnerability scanning: passed;
- Markdown lint and `npm audit --audit-level=moderate`: passed with zero
  vulnerabilities; and
- exact wheel verification: passed for
  `sidekick_usages-0.6.0-py3-none-any.whl`.

Installation evidence:

- `uv sync --all-groups` completed successfully;
- `uv pip install -e .` refreshed the repository environment;
- `uv tool install --force --editable .` refreshed the global uv tool;
- both interpreters import `usage/narrow_render.py` from this working tree;
- repository and global help output are identical; and
- both real dashboards rendered two year-bearing provider footers without a
  traceback or obsolete `local CLI`/`known tokens` wording.

The latest global verification rendered:

```text
917,952,231 tokens  ·  since Dec 28, 2025
7,517,762,237 tokens  ·  since Apr 7, 2026
```

The expected exit code was `1` because one saved Codex account still requires
authentication recovery; the dashboard and recovery guidance rendered before
that typed manual-action result.
