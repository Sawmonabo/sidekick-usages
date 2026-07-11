# Usage TUI Redesign ("Framed Panels") Implementation Plan

> **Subsequent token-activity correction (2026-07-10):** This executed plan
> accurately records the earlier output-only local implementation, but that
> metric, the top-level `lifetime.py` owner, and the Codex rollout cache are
> superseded by the tracked
> [token activity accuracy plan](./2026-07-10-token-activity-accuracy.md).
> Task excerpts below remain historical execution evidence, not current
> implementation authority.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task by task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-account braille-bar usage display with a
provider-grouped heatmap of framed panels, add a per-provider lifetime-output
footer, and provide an explicit manual plan override.

**Architecture:** A new pure renderer in `render.py` takes *all*
`(Account, UsageReport)` pairs plus a per-provider lifetime lookup. It emits a
responsive robot masthead → one rounded `Panel` per provider (provider-local
account count + heat-tile matrix + lifetime footer) → legend. A new
`lifetime.py` module reads local stats files: Claude is pre-aggregated and
Codex rollouts use an incremental cache. It returns `(output_total, since)` per
provider. The renderer *receives* those values and never calls the data layer.
The `check` caller collects successful reports during its existing fetch loop
and renders the grouped overview once. A small `set-plan` command handles
plans the usage API cannot introspect.

**Tech Stack:** Python ≥3.14, Rich ≥13.9, Typer/Click, `uv` for tooling,
`pytest` ≥9, Ruff, and `ty`.

## Global Constraints

> **Post-implementation refinements:** The final approved renderer uses the
> responsive robot masthead, provider-local account counts, `(1,2)` panel
> padding, and an 85-column binding floor. The original task code excerpts
> below preserve the implementation sequence at the time they were written;
> where an excerpt still shows the earlier global summary, `(0,0)` padding, or
> 80-column prototype, this refinement and the approved design specification
> supersede it. Current behavior is pinned by the focused verification at the
> end of this plan.

Every task implicitly includes these (copied from the spec):

- **Python ≥3.14.** Run everything through `uv run`, including pytest, Ruff,
  and `ty`. Bare `python3` is the wrong interpreter (3.10). PEP 758
  `except A, B:` syntax is valid here; do not "fix" it.
- **Ruff line length = 79**, double quotes, target `py314`, and first-party
  import root `sidekick_usages`. Run format and check before every commit.
- **`ty` strict type checking** (errors on warnings). Run `uv run ty check`
  before every commit. Fully type all new code.
- **pytest** config: `testpaths=["tests"]`, `--strict-markers`, and
  `--strict-config`. Test files are `tests/test_*.py`.
- **Runtime data, verbatim.** Account labels and plan tags render exactly as
  stored. Tests use reserved fixtures. Suppress the plan tag only when empty or
  `"unknown"`.
- **Width — hard floor 85 columns.** The reserved worst case (Codex + "Spark"
  block + 30-character name) must render at `Console(width=85)` with **no
  wrapped physical line** and the longest name intact. Below 85, deliberately
  use the legacy per-account view; **never silently wrap**.
- **Branch:** `feat/usage-tui-redesign`. Commit messages end with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Manual-plan commits stay separate** from rendering commits. Task 8 is
  self-contained.

---

## File Structure

- **`src/sidekick_usages/render.py`** (modify) — heat helpers, compact-reset
  helpers, window classifier, `usage_overview(...)`, private panel builders,
  and the width formula. Retain `usage_report` / `account_header` for error,
  empty, and fallback rendering.
- **`src/sidekick_usages/lifetime.py`** (create) — `format_tokens`,
  `format_since`, `claude_lifetime_output`, `codex_lifetime_output`, and the
  incremental Codex cache. Do not import Rich or `cli`/`render`.
- **`src/sidekick_usages/cli.py`** (modify) — `AppContext.collected`, a
  `_collect` helper, grouped `_do_check` rendering, lifetime threading, and
  `set-plan`.
- **`tests/test_render.py`** (create) — heat bands, tiles, compact reset,
  classification, grouped rendering, the **85-column width guard**, and the
  fallback path.
- **`tests/test_lifetime.py`** (create) — token and `since` formatting, Claude
  sum, Codex sum, and cache behavior against fixture files.
- **`tests/test_cli_setplan.py`** (create) — `set-plan` behavior.

Setup note: confirm the branch before Task 1. The command
`git rev-parse --abbrev-ref HEAD` should print `feat/usage-tui-redesign`. If
not, create that branch from `main` before editing.

---

### Task 1: Heat band + tile cell

**Files:**
- Modify: `src/sidekick_usages/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Produces: `_HEAT_BANDS: list[tuple[int, str, str]]`, `_IDLE_FG: str`,
  `_TILE_WIDTH: int`, `_heat_band(pct: int) -> tuple[str, str] | None`, and
  `_heat_tile(pct: int) -> rich.text.Text`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
from rich.text import Text

from sidekick_usages import render


def test_heat_band_picks_inclusive_lower_bounds():
    assert render._heat_band(90) == ("#ffe6e6", "#b03030")
    assert render._heat_band(89) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(70) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(40) == ("#e2fbff", "#1b6a87")
    assert render._heat_band(1) == ("#dfffe9", "#1d5e35")
    assert render._heat_band(0) is None


def test_heat_tile_zero_is_centered_dot_no_fill():
    tile = render._heat_tile(0)
    assert tile.plain == f"{'·':^{render._TILE_WIDTH}}"
    assert tile.style == render._IDLE_FG


def test_heat_tile_nonzero_is_centered_percent_on_band():
    tile = render._heat_tile(94)
    assert tile.plain == f"{'94%':^{render._TILE_WIDTH}}"
    assert tile.style == "#ffe6e6 on #b03030"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_render.py -k heat -v`
Expected: FAIL (`AttributeError: module ... has no attribute '_heat_band'`).

- [ ] **Step 3: Write minimal implementation**

Add near the existing color constants in `render.py`:

```python
#: Heat bands as (lower-bound-inclusive percent, fg hex, bg hex).
#: Thresholds match the legacy ``_utilization_color`` bands.
_HEAT_BANDS: list[tuple[int, str, str]] = [
    (90, "#ffe6e6", "#b03030"),
    (70, "#fff4e0", "#9c6f12"),
    (40, "#e2fbff", "#1b6a87"),
    (1, "#dfffe9", "#1d5e35"),
]

#: Foreground for a zero-utilization (idle) cell — no fill.
_IDLE_FG = "grey39"

#: Fixed width of one window tile.
_TILE_WIDTH = 6


def _heat_band(pct: int) -> tuple[str, str] | None:
    """Return ``(fg, bg)`` for a utilization percent, or None at 0.

    :param pct: Rounded utilization 0-100.
    :return: ``(fg_hex, bg_hex)`` for a filled tile, or ``None`` for
        a zero cell (rendered as a fill-less ``·``).
    """
    for threshold, fg, bg in _HEAT_BANDS:
        if pct >= threshold:
            return (fg, bg)
    return None


def _heat_tile(pct: int) -> Text:
    """Build one fixed-width heat tile.

    :param pct: Rounded utilization 0-100.
    :return: A ``Text`` of width ``_TILE_WIDTH``: a faint centered
        ``·`` at 0, otherwise ``NN%`` centered on the band color.
    """
    band = _heat_band(pct)
    if band is None:
        return Text(f"{'·':^{_TILE_WIDTH}}", style=_IDLE_FG)
    fg, bg = band
    return Text(f"{f'{pct}%':^{_TILE_WIDTH}}", style=f"{fg} on {bg}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_render.py -k heat -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/render.py tests/test_render.py
uv run ruff check src/sidekick_usages/render.py tests/test_render.py
uv run ty check
git add src/sidekick_usages/render.py tests/test_render.py
git commit -m "feat(render): add heat band + tile helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Compact reset cell

**Files:**
- Modify: `src/sidekick_usages/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `_TILE_WIDTH` (Task 1).
- Produces: `_format_reset_compact(iso: str | None) -> str` and
  `_reset_cell(iso: str | None) -> rich.text.Text`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py (append)
from datetime import UTC, datetime, timedelta


def _iso_in(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def test_format_reset_compact_buckets():
    assert render._format_reset_compact(None) == ""
    assert render._format_reset_compact("not-a-date") == ""
    assert render._format_reset_compact(_iso_in(minutes=-5)) == "now"
    assert render._format_reset_compact(_iso_in(minutes=45)) == "45m"
    assert render._format_reset_compact(_iso_in(hours=3, minutes=50)) == "3h 50m"
    assert render._format_reset_compact(_iso_in(days=1, hours=15)) == "1d 15h"


def test_reset_cell_is_centered_dim():
    cell = render._reset_cell(_iso_in(hours=3, minutes=50))
    assert cell.plain == f"{'3h 50m':^{render._TILE_WIDTH}}"
    assert cell.style == "grey42"
    assert render._reset_cell(None).plain == f"{'':^{render._TILE_WIDTH}}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_render.py -k reset -v`
Expected: FAIL (`_format_reset_compact` undefined).

- [ ] **Step 3: Write minimal implementation**

Add to `render.py`. Reuse the existing `_SECONDS_PER_HOUR`,
`_SECONDS_PER_DAY`, and `datetime`/`UTC` imports.

```python
def _format_reset_compact(iso: str | None) -> str:
    """Compact relative countdown: ``45m`` / ``3h 50m`` / ``1d 15h``.

    No ``↻`` glyph and no absolute timestamp (those are dropped from
    the matrix per the spec).

    :param iso: ISO-8601 timestamp or ``None``.
    :return: A compact string, ``"now"`` if already due, or ``""``
        when missing/unparseable.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    secs = int((dt - datetime.now(UTC)).total_seconds())
    if secs <= 0:
        return "now"
    if secs < _SECONDS_PER_HOUR:
        return f"{secs // 60}m"
    if secs < _SECONDS_PER_DAY:
        hours, minutes = divmod(secs // 60, 60)
        return f"{hours}h {minutes}m"
    days, remainder = divmod(secs, _SECONDS_PER_DAY)
    return f"{days}d {remainder // _SECONDS_PER_HOUR}h"


def _reset_cell(iso: str | None) -> Text:
    """Build one fixed-width, dim, centered reset-countdown cell.

    :param iso: ISO-8601 timestamp or ``None``.
    :return: A ``Text`` of width ``_TILE_WIDTH``.
    """
    return Text(
        f"{_format_reset_compact(iso):^{_TILE_WIDTH}}",
        style="grey42",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_render.py -k reset -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/render.py tests/test_render.py
uv run ruff check src/sidekick_usages/render.py tests/test_render.py
uv run ty check
git add src/sidekick_usages/render.py tests/test_render.py
git commit -m "feat(render): add compact reset-countdown cell

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Window classification

**Files:**
- Modify: `src/sidekick_usages/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Produces: `_classify_window(name: str) -> tuple[str, str]`, returning
  `(length, group)`, and `_length_hours(length: str) -> int`.

Live window names confirmed: Claude emits `"5h"`, `"7d"`, `"7d Opus"`, and
`"7d OAuth"` (group label *after* the length). Codex emits `"5h"`, `"7d"`,
`"Spark 5h"`, and `"Spark 7d"` (group *before*). The `\d+[hd]`-anywhere rule
handles both positions.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py (append)
import pytest


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("5h", ("5h", "")),
        ("7d", ("7d", "")),
        ("7d Opus", ("7d", "Opus")),
        ("7d OAuth", ("7d", "OAuth")),
        ("Spark 5h", ("5h", "Spark")),
        ("Spark 7d", ("7d", "Spark")),
    ],
)
def test_classify_window(name, expected):
    assert render._classify_window(name) == expected


def test_length_hours_orders_5h_before_7d():
    assert render._length_hours("5h") == 5
    assert render._length_hours("7d") == 168
    assert render._length_hours("5h") < render._length_hours("7d")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_render.py -k "classify or length_hours" -v`
Expected: FAIL (`_classify_window` undefined).

- [ ] **Step 3: Write minimal implementation**

Add `import re` to the top of `render.py`, then:

```python
#: Matches a window length token such as ``5h`` or ``7d``.
_LENGTH_RE = re.compile(r"\d+[hd]")


def _classify_window(name: str) -> tuple[str, str]:
    """Split a window name into ``(length, group)``.

    The length is the first ``\\d+[hd]`` token anywhere in the name;
    the remaining text, trimmed, is the group label (``""`` = the
    provider's main limit; e.g. ``"Spark"`` / ``"Opus"`` for named
    groups). No hardcoded provider tables.

    :param name: Raw ``UsageWindow.name``.
    :return: ``(length, group)``.
    """
    match = _LENGTH_RE.search(name)
    if match is None:
        return (name.strip(), "")
    length = match.group(0)
    group = (name[: match.start()] + name[match.end() :]).strip()
    return (length, group)


def _length_hours(length: str) -> int:
    """Return a sort key (in hours) for a length token.

    :param length: A token like ``"5h"`` or ``"7d"``.
    :return: Hours (``"7d"`` -> 168), or 0 if unparseable.
    """
    match = _LENGTH_RE.fullmatch(length)
    if match is None:
        return 0
    value = int(length[:-1])
    return value * 24 if length[-1] == "d" else value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_render.py -k "classify or length_hours" -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/render.py tests/test_render.py
uv run ruff check src/sidekick_usages/render.py tests/test_render.py
uv run ty check
git add src/sidekick_usages/render.py tests/test_render.py
git commit -m "feat(render): add data-driven window classifier

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Lifetime formatting + Claude source

**Files:**
- Create: `src/sidekick_usages/lifetime.py`
- Test: `tests/test_lifetime.py`

**Interfaces:**
- Produces: `format_tokens(n: int) -> str`,
  `format_since(value: str | None) -> str`,
  `claude_lifetime_output() -> tuple[int, str | None]`, and overridable module
  constant `_CLAUDE_STATS_FILE: pathlib.Path`.

Confirmed Claude schema (`~/.claude/stats-cache.json`): `modelUsage` maps each
model to `{"outputTokens": int, ...}`. Lifetime output is the sum of
`outputTokens`; `firstSessionDate` is the `since` anchor.

> **Spec deviation (intentional, empirically verified):** Spec §6 says Claude's
> `since` value is the earliest date in `dailyModelTokens`. This plan reads the
> top-level `firstSessionDate` instead. The observed
> `firstSessionDate = "2025-12-28T23:26:31.884Z"`; `dailyModelTokens` is a list
> of `{"date", "tokensByModel"}` whose earliest `date` is also `"2025-12-28"`,
> as is `dailyActivity[0].date`. Both yield
> `format_since(...) == "Dec 28"`, matching the approved mockup. The O(1)
> single-field read does not depend on a daily-series retention window that
> could be truncated independently. `firstSessionDate` is the first *session*
> date, not the account-creation date (`oauthAccount.accountCreatedAt` is
> earlier), so it honestly bounds the `modelUsage` total.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lifetime.py
import json
from pathlib import Path

from sidekick_usages import lifetime


def test_format_tokens():
    assert lifetime.format_tokens(0) == "0"
    assert lifetime.format_tokens(950) == "950"
    assert lifetime.format_tokens(12_300) == "12K"
    assert lifetime.format_tokens(424_000_000) == "424M"
    assert lifetime.format_tokens(1_500_000_000) == "1.5B"


def test_format_since():
    assert lifetime.format_since(None) == ""
    assert lifetime.format_since("2026-03-30") == "Mar 30"
    assert lifetime.format_since("2025-12-28T10:00:00Z") == "Dec 28"
    assert lifetime.format_since("garbage") == "garbage"


def test_claude_lifetime_output_sums_model_output(tmp_path, monkeypatch):
    stats = tmp_path / "stats-cache.json"
    stats.write_text(
        json.dumps(
            {
                "firstSessionDate": "2025-12-28T00:00:00Z",
                "modelUsage": {
                    "model-a": {"outputTokens": 100, "inputTokens": 9},
                    "model-b": {"outputTokens": 200},
                },
            }
        )
    )
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", stats)
    assert lifetime.claude_lifetime_output() == (300, "2025-12-28T00:00:00Z")


def test_claude_lifetime_output_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", tmp_path / "none.json")
    assert lifetime.claude_lifetime_output() == (0, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lifetime.py -v`
Expected: FAIL (`ModuleNotFoundError: sidekick_usages.lifetime`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/sidekick_usages/lifetime.py
"""Per-provider lifetime OUTPUT-token aggregation (leak-free).

Reads local, read-only stats: Claude's pre-aggregated
``stats-cache.json`` and (in Task 5) the Codex rollout logs. Returns
``(output_total, since)`` per provider. Output tokens are the only
cross-provider-comparable measure (Claude reports cache-read
separately; Codex folds cached tokens into ``input_tokens``).
"""

import json
from datetime import datetime
from pathlib import Path

#: Claude's machine-wide pre-aggregated stats (all Claude Code usage
#: on this machine, not just sidekick-managed accounts).
_CLAUDE_STATS_FILE = Path.home() / ".claude" / "stats-cache.json"


def format_tokens(n: int) -> str:
    """Render a token count compactly (``424M``, ``1.5B``).

    :param n: Token count.
    :return: A short human string.
    """
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def format_since(value: str | None) -> str:
    """Render a date as ``Mon D`` (e.g. ``Dec 28``).

    :param value: ISO date/datetime string, or ``None``.
    :return: ``"Mon D"``, ``""`` for ``None``, or the raw string if
        unparseable.
    """
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    return f"{dt:%b} {dt.day}"


def claude_lifetime_output() -> tuple[int, str | None]:
    """Sum Claude lifetime output tokens across all local models.

    :return: ``(output_total, since)`` — ``(0, None)`` if the stats
        file is missing or unreadable.
    """
    try:
        data = json.loads(_CLAUDE_STATS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return (0, None)
    total = 0
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict):
        for usage in model_usage.values():
            if isinstance(usage, dict):
                out = usage.get("outputTokens")
                if isinstance(out, int):
                    total += out
    since = data.get("firstSessionDate")
    return (total, since if isinstance(since, str) else None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lifetime.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/lifetime.py tests/test_lifetime.py
uv run ruff check src/sidekick_usages/lifetime.py tests/test_lifetime.py
uv run ty check
git add src/sidekick_usages/lifetime.py tests/test_lifetime.py
git commit -m "feat(lifetime): add token formatting + Claude output sum

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Codex lifetime source + incremental cache

**Files:**
- Modify: `src/sidekick_usages/lifetime.py`
- Test: `tests/test_lifetime.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `codex_lifetime_output() -> tuple[int, str | None]` and overridable
  constants `_CODEX_SESSIONS_DIR: Path` and `_CODEX_CACHE_FILE: Path`.

Confirmed Codex schema: each `~/.codex/sessions/**/rollout-*.jsonl` line may
carry `payload.info.total_token_usage.output_tokens`, the session's repeated
and growing *cumulative* total. Take the **maximum** per file and sum across
files. `since` is the earliest rollout date from
`rollout-YYYY-MM-DD...`. About 1500 files exist, so cache by filename and
modification time and re-read only new or changed files.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lifetime.py (append)
def _rollout(dir_, date, outputs):
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"rollout-{date}T00-00-00-abc.jsonl"
    lines = [
        json.dumps(
            {"payload": {"info": {"total_token_usage": {"output_tokens": o}}}}
        )
        for o in outputs
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_codex_lifetime_sums_per_file_max(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _rollout(sessions / "2026" / "03", "2026-03-30", [10, 50, 30])  # max 50
    _rollout(sessions / "2026" / "06", "2026-06-18", [5, 200])      # max 200
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)
    monkeypatch.setattr(lifetime, "_CODEX_CACHE_FILE", tmp_path / "c.json")
    assert lifetime.codex_lifetime_output() == (250, "2026-03-30")


def test_codex_lifetime_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(lifetime, "_CODEX_CACHE_FILE", tmp_path / "c.json")
    assert lifetime.codex_lifetime_output() == (0, None)


def test_codex_lifetime_uses_cache(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    path = _rollout(sessions, "2026-03-30", [42])
    cache_file = tmp_path / "c.json"
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)
    monkeypatch.setattr(lifetime, "_CODEX_CACHE_FILE", cache_file)

    assert lifetime.codex_lifetime_output() == (42, "2026-03-30")
    assert cache_file.exists()
    # Corrupt the file body but keep mtime: a cache hit ignores it.
    import os

    st = path.stat()
    path.write_text("not json\n")
    os.utime(path, (st.st_atime, st.st_mtime))
    assert lifetime.codex_lifetime_output() == (42, "2026-03-30")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lifetime.py -k codex -v`
Expected: FAIL (`codex_lifetime_output` undefined).

- [ ] **Step 3: Write minimal implementation**

Add to `lifetime.py` (the `Path`/`json` imports already exist):

```python
#: Codex session logs and the sidekick-side incremental cache.
_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_CODEX_CACHE_FILE = (
    Path.home() / ".config" / "sidekick-usages" / "codex-lifetime-cache.json"
)


def _total_token_usage(record: object) -> dict | None:
    """Return ``payload.info.total_token_usage`` if present."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    return usage if isinstance(usage, dict) else None


def _max_output_in_rollout(path: Path) -> int:
    """Return the max cumulative ``output_tokens`` in one rollout."""
    best = 0
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = _total_token_usage(record)
                if usage is not None:
                    out = usage.get("output_tokens")
                    if isinstance(out, int) and out > best:
                        best = out
    except OSError:
        return 0
    return best


def _rollout_date(filename: str) -> str | None:
    """Extract ``YYYY-MM-DD`` from a ``rollout-...`` filename."""
    stem = filename.removeprefix("rollout-")
    date = stem[:10]
    return date if len(date) == 10 and date[4] == "-" else None


def _load_codex_cache() -> dict:
    try:
        return json.loads(_CODEX_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_codex_cache(cache: dict) -> None:
    try:
        _CODEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CODEX_CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def codex_lifetime_output() -> tuple[int, str | None]:
    """Sum Codex lifetime output tokens across all rollout logs.

    Per file uses the maximum cumulative ``output_tokens`` (the
    session total) and sums across files. Closed sessions are
    immutable, so results are cached per filename+mtime; only new or
    still-growing files are re-read.

    :return: ``(output_total, since)`` — ``(0, None)`` if no logs.
    """
    files = sorted(_CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"))
    if not files:
        return (0, None)
    cache = _load_codex_cache()
    entries = cache.get("files")
    if not isinstance(entries, dict):
        entries = {}
    total = 0
    changed = False
    for path in files:
        key = path.name
        mtime = path.stat().st_mtime
        cached = entries.get(key)
        if isinstance(cached, dict) and cached.get("mtime") == mtime:
            output = cached.get("output", 0)
        else:
            output = _max_output_in_rollout(path)
            entries[key] = {"mtime": mtime, "output": output}
            changed = True
        total += output
    if changed:
        _save_codex_cache({"files": entries})
    return (total, _rollout_date(files[0].name))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lifetime.py -v`
Expected: PASS (all lifetime tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/lifetime.py tests/test_lifetime.py
uv run ruff check src/sidekick_usages/lifetime.py tests/test_lifetime.py
uv run ty check
git add src/sidekick_usages/lifetime.py tests/test_lifetime.py
git commit -m "feat(lifetime): add cached Codex output summing

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Provider-grouped renderer + width guard

**Files:**
- Modify: `src/sidekick_usages/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `_heat_tile`, `_reset_cell`, `_classify_window`, `_length_hours`,
  and `_TILE_WIDTH` from Tasks 1–3; `format_tokens` and `format_since` from
  Task 4.
- Produces:
  `usage_overview(pairs: list[tuple[Account, UsageReport]],
  lifetime: dict[str, tuple[int, str | None]], *, width: int) -> RenderableType`.

`pairs` retain caller iteration order; the renderer groups by provider while
preserving first-seen order. `lifetime` maps
`provider_id -> (output_total, since)`. The renderer imports only the two pure
formatters from the lifetime module, never its data-collection functions.

The required width includes the measured table, rounded borders, and approved
two-column horizontal side margins. The reserved Codex + Spark fixture binds
at 85 columns. Below the binding provider's width, use the legacy stacked
view.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_render.py (append)
import io

from rich.console import Console

from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account


def _acct(label, provider="claude", plan="max"):
    return Account(
        label=label,
        provider_id=provider,
        access_token="t",
        plan=plan,
    )


def _report(*windows):
    return UsageReport(
        windows=[UsageWindow(*w) for w in windows],
        plan="max",
        raw={},
    )


def _worst_case_pairs():
    # 3 Claude + 2 Codex; the reserved 30-character name + Spark block is
    # the synthetic binding-width fixture.
    iso = _iso_in(hours=3, minutes=50)
    claude = [
        (_acct("long.account.name@example.test"), _report(("5h", 94, iso), ("7d", 61, iso))),
        (_acct("long.account.name@example.test", plan="team"), _report(("5h", 12, iso), ("7d", 73, iso))),
        (_acct("long.account.name@example.test"), _report(("5h", 40, iso), ("7d", 5, iso))),
    ]
    codex = [
        (_acct("long.account.name@example.test", "codex", "pro"), _report(("5h", 8, iso), ("7d", 45, iso), ("Spark 5h", 0, iso), ("Spark 7d", 0, iso))),
        (_acct("long.account.name@example.test", "codex", "pro"), _report(("5h", 0, iso), ("7d", 0, iso), ("Spark 5h", 0, iso), ("Spark 7d", 0, iso))),
    ]
    return claude + codex


_LIFETIME = {"claude": (424_000_000, "2025-12-28"), "codex": (212_000_000, "2026-03-30")}


def _render_at(width, pairs):
    console = Console(width=width, file=io.StringIO())
    console.print(render.usage_overview(pairs, _LIFETIME, width=width))
    return console.file.getvalue()


def test_width_guard_fits_80_columns():
    out = _render_at(80, _worst_case_pairs())
    lines = out.split("\n")
    assert max(len(line) for line in lines) <= 80
    # longest name intact on a single physical line, not elided
    assert any("long.account.name@example.test" in line for line in lines)


def test_overview_shows_titles_and_lifetime():
    out = _render_at(80, _worst_case_pairs())
    assert "CLAUDE" in out
    assert "CODEX" in out
    assert "424M output" in out
    assert "since Mar 30" in out
    assert "Spark" in out


def test_overview_degrades_below_80_to_legacy():
    # Below the binding panel width the renderer falls back to the
    # legacy stacked view instead of squeezing/wrapping the panels.
    # Discriminator: the uppercase panel title only exists on the
    # panel path; the legacy tag uses the lowercase provider id.
    out = _render_at(70, _worst_case_pairs())
    assert "CLAUDE" not in out
    assert "long.account.name@example.test" in out


def test_overview_empty_pairs():
    out = _render_at(80, [])
    assert "No usage" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render.py -k overview -v`
Expected: FAIL (`usage_overview` undefined).

- [ ] **Step 3: Write minimal implementation**

Add the imports at the top of `render.py`:

```python
from rich.panel import Panel
from rich.rule import Rule

from sidekick_usages.lifetime import format_since, format_tokens
```

Then add the renderer (uses `PROVIDER_COLORS`, `PLAN_COLORS` already defined):

```python
def _dot(provider_id: str) -> Text:
    return Text("●", style=PROVIDER_COLORS.get(provider_id, "dim"))


def _plan_text(acct: Account) -> Text:
    """Plan chip, suppressed for empty/unknown (matches legacy tag)."""
    if not acct.plan or acct.plan == "unknown":
        return Text("")
    return Text(acct.plan, style=PLAN_COLORS.get(acct.plan, "grey42"))


def _panel_columns(
    reports: list[UsageReport],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Derive the column model for one provider from live data.

    :return: ``(primary_lengths, named_groups)`` where primary are the
        main-group lengths (aligned ``5h``/``7d`` columns) and each
        named group is ``(label, lengths)``. Lengths sorted ascending.
    """
    main: dict[str, int] = {}
    groups: dict[str, dict[str, int]] = {}
    for report in reports:
        for window in report.windows:
            length, group = _classify_window(window.name)
            hours = _length_hours(length)
            if group == "":
                main[length] = hours
            else:
                groups.setdefault(group, {})[length] = hours
    primary = sorted(main, key=lambda x: main[x])
    named = [
        (group, sorted(lengths, key=lambda x: lengths[x]))
        for group, lengths in sorted(groups.items())
    ]
    return primary, named


def _window_index(report: UsageReport) -> dict[tuple[str, str], UsageWindow]:
    """Map ``(group, length) -> window`` for one report."""
    index: dict[tuple[str, str], UsageWindow] = {}
    for window in report.windows:
        length, group = _classify_window(window.name)
        index[(group, length)] = window
    return index


def _util_cell(window: UsageWindow | None) -> Text:
    if window is None:
        return Text("")
    return _heat_tile(round(window.utilization))


def _reset_or_blank(window: UsageWindow | None) -> Text:
    if window is None:
        return Text("")
    return _reset_cell(window.resets_at)


def _panel_width(
    namew: int,
    primary: list[str],
    named: list[tuple[str, list[str]]],
) -> int:
    """Total rendered width of one provider panel (borders included)."""
    n_cols = 3 + len(primary)
    sum_w = 1 + namew + 4 + _TILE_WIDTH * len(primary)
    for _group, lengths in named:
        n_cols += 1 + len(lengths)
        sum_w += 5 + _TILE_WIDTH * len(lengths)
    inner = sum_w + 2 * (n_cols - 1)
    return inner + 2  # rounded-panel left/right border


def _build_table(
    namew: int,
    primary: list[str],
    named: list[tuple[str, list[str]]],
) -> Table:
    table = Table(
        box=None,
        show_header=False,  # header is added manually as a styled row
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column(width=1)  # dot
    table.add_column(width=namew)  # name
    table.add_column(width=4)  # plan
    for _length in primary:
        table.add_column(width=_TILE_WIDTH, justify="center")
    for _group, lengths in named:
        table.add_column(width=5, justify="center")  # group label
        for _length in lengths:
            table.add_column(width=_TILE_WIDTH, justify="center")
    header: list[Text] = [Text(""), Text(""), Text("")]
    for length in primary:
        header.append(Text(length, style="grey42"))
    for group, lengths in named:
        header.append(Text(group, style="grey46"))
        for length in lengths:
            header.append(Text(length, style="grey42"))
    table.add_row(*header)
    return table


def _provider_panel(
    provider_id: str,
    pairs: list[tuple[Account, UsageReport]],
    namew: int,
    prov_lifetime: tuple[int, str | None] | None,
) -> Panel:
    primary, named = _panel_columns([r for _, r in pairs])
    table = _build_table(namew, primary, named)
    for acct, report in pairs:
        index = _window_index(report)
        util_row: list[Text] = [_dot(provider_id), Text(acct.label, style="grey85"), _plan_text(acct)]
        reset_row: list[Text] = [Text(""), Text(""), Text("")]
        for length in primary:
            window = index.get(("", length))
            util_row.append(_util_cell(window))
            reset_row.append(_reset_or_blank(window))
        for group, lengths in named:
            util_row.append(Text(""))
            reset_row.append(Text(""))
            for length in lengths:
                window = index.get((group, length))
                util_row.append(_util_cell(window))
                reset_row.append(_reset_or_blank(window))
        table.add_row(*util_row)
        table.add_row(*reset_row)
        table.add_row(*([Text("")] * len(util_row)))
    color = PROVIDER_COLORS.get(provider_id, "white")
    title = Text(f" {provider_id.upper()} ", style=f"bold {color}")
    subtitle = None
    if prov_lifetime is not None:
        total, since = prov_lifetime
        subtitle = Text()
        subtitle.append(f"{format_tokens(total)} output", style="grey54")
        since_str = format_since(since)
        if since_str:
            subtitle.append(f"  ·  since {since_str} ", style="grey35")
    return Panel(
        table,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=color,
        padding=(0, 0),
        expand=True,
    )


def _top_strip(n_accounts: int, n_providers: int) -> Group:
    title = Text()
    title.append("sidekick", style="bold grey85")
    title.append(" usages", style="bold grey62")
    summary = Text(
        f"{n_accounts} accounts · {n_providers} providers",
        style="grey42",
    )
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    grid.add_row(title, summary)
    return Group(grid, Rule(style="grey23"))


def _legend() -> Text:
    legend = Text()
    for label, sample in (("<40", 20), ("40-69", 55), ("70-89", 80), ("≥90", 95)):
        band = _heat_band(sample)
        fg, bg = band if band else (_IDLE_FG, "default")
        legend.append(f" {label} ", style=f"{fg} on {bg}")
        legend.append("  ")
    legend.append("   dim = resets in", style="grey42")
    return legend


def _provider_order(pairs: list[tuple[Account, UsageReport]]) -> list[str]:
    order: list[str] = []
    for acct, _ in pairs:
        if acct.provider_id not in order:
            order.append(acct.provider_id)
    return order


def _legacy_overview(
    pairs: list[tuple[Account, UsageReport]],
) -> RenderableType:
    """Stacked per-account fallback for narrow terminals (no wrap)."""
    blocks: list[RenderableType] = []
    for index, (acct, report) in enumerate(pairs):
        if index:
            blocks.append(Text(""))
        blocks.append(usage_report(acct, report))
    return Group(*blocks)


def usage_overview(
    pairs: list[tuple[Account, UsageReport]],
    lifetime: dict[str, tuple[int, str | None]],
    *,
    width: int,
) -> RenderableType:
    """Render all accounts as provider-grouped framed heat panels.

    :param pairs: ``(Account, UsageReport)`` for every fetched account.
    :param lifetime: ``provider_id -> (output_total, since)``.
    :param width: Target terminal width; below the binding panel
        width the layout degrades to the legacy stacked view.
    :return: A Rich renderable.
    """
    if not pairs:
        return Text("No usage to display.", style="dim")
    namew = max(len(acct.label) for acct, _ in pairs)
    order = _provider_order(pairs)
    required = 0
    for provider_id in order:
        reports = [r for a, r in pairs if a.provider_id == provider_id]
        primary, named = _panel_columns(reports)
        required = max(required, _panel_width(namew, primary, named))
    if width < required:
        return _legacy_overview(pairs)
    parts: list[RenderableType] = [_top_strip(len(pairs), len(order)), Text("")]
    for provider_id in order:
        prov_pairs = [(a, r) for a, r in pairs if a.provider_id == provider_id]
        parts.append(
            _provider_panel(
                provider_id, prov_pairs, namew, lifetime.get(provider_id)
            )
        )
        parts.append(Text(""))
    parts.append(_legend())
    return Group(*parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render.py -v`
Expected: PASS (all render tests, including the width guard).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/render.py tests/test_render.py
uv run ruff check src/sidekick_usages/render.py tests/test_render.py
uv run ty check
git add src/sidekick_usages/render.py tests/test_render.py
git commit -m "feat(render): add provider-grouped heat-panel overview

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Wire grouped rendering into `check`

**Files:**
- Modify: `src/sidekick_usages/cli.py` (`AppContext`, `_do_check`,
  `_fetch_usage_and_render`, and `_handle_runtime_forbidden`).
- Test: `tests/test_cli_refresh.py` (regression only; it must stay green).

**Interfaces:**
- Consumes: `usage_overview` from Task 6 plus `claude_lifetime_output` and
  `codex_lifetime_output` from Tasks 4–5.
- Produces: `AppContext.collected`, `_collect(acct, report)`.

Design: keep every fetch helper's `bool` contract intact so all eight existing
`cli._fetch_and_render(...) is True/False` assertions remain unchanged. The
two success sites *collect* instead of printing; `_do_check` renders the
grouped overview once after the loop. Errors still print inline.

- [ ] **Step 1: Add a focused regression test**

```python
# tests/test_cli_refresh.py (append)
def test_check_renders_grouped_overview(tmp_path, monkeypatch):
    """`check` collects successes and prints one grouped overview."""
    import sidekick_usages.cli as cli_mod
    monkeypatch.setattr(cli_mod, "claude_lifetime_output", lambda: (1, None))
    acct = _acct(plan="max")
    provider = _FakeProvider(fetch_results=[_report()])
    _, stdout, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 0
    assert "CLAUDE" in stdout.getvalue()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_refresh.py -k grouped -v`
Expected: FAIL (`claude_lifetime_output` not importable in cli, or "CLAUDE" absent).

- [ ] **Step 3: Implement the wiring**

In `cli.py`, extend the dataclass import and add the field. Change the existing
dataclass import to:

```python
from dataclasses import dataclass, field
```

Add/replace imports near the top:

```python
from sidekick_usages.lifetime import (
    claude_lifetime_output,
    codex_lifetime_output,
)
from sidekick_usages.render import account_header, usage_overview
from sidekick_usages.report import UsageReport
```

Replace the existing `account_header, usage_report` renderer import with the
`account_header, usage_overview` import above. Both old call sites become
`_collect`, so retaining `usage_report` would trigger Ruff F401. Add the
`UsageReport` import for the new annotations.

Add the field to `AppContext` (after `heartbeat_providers`, line ~108):

```python
    collected: list[tuple[Account, UsageReport]] = field(default_factory=list)
```

Add a collector helper near `_fetch_and_render`:

```python
def _collect(acct: Account, report: UsageReport) -> None:
    """Stash a successful report for the end-of-run grouped render."""
    _get_ctx().collected.append((acct, report))
```

Replace the success-print in `_fetch_usage_and_render` (line 401) — change:

```python
    app_ctx.console.print(usage_report(acct, report))
    return True
```

to:

```python
    _collect(acct, report)
    return True
```

Replace the success-print in `_handle_runtime_forbidden` (line 350) — change:

```python
        app_ctx.console.print(usage_report(acct, report))
        return True
```

to:

```python
        _collect(acct, report)
        return True
```

Rewrite `_do_check` (lines 285-306):

```python
def _do_check() -> None:
    """Fetch all (filtered) accounts and render the grouped overview.

    Exits with code 1 if any account failed.
    """
    app_ctx = _get_ctx()
    app_ctx.collected.clear()
    accounts = list(app_ctx.store)
    if app_ctx.only:
        accounts = [a for a in accounts if a.provider_id == app_ctx.only]
    if not accounts:
        _print_no_accounts(app_ctx.only)
        raise typer.Exit(code=1)

    exit_code = 0
    for acct in accounts:
        if not _fetch_and_render(acct):
            exit_code = 1

    if app_ctx.collected:
        app_ctx.console.print(
            usage_overview(
                app_ctx.collected,
                _lifetime_for(app_ctx.collected),
                width=app_ctx.console.size.width,
            )
        )
    if exit_code:
        raise typer.Exit(code=exit_code)


def _lifetime_for(
    pairs: list[tuple[Account, UsageReport]],
) -> dict[str, tuple[int, str | None]]:
    """Look up lifetime output per provider present in ``pairs``."""
    sources = {
        "claude": claude_lifetime_output,
        "codex": codex_lifetime_output,
    }
    providers = {acct.provider_id for acct, _ in pairs}
    return {
        provider_id: source()
        for provider_id, source in sources.items()
        if provider_id in providers
    }
```

After this task, `usage_report` is no longer referenced in `cli.py`; it remains
inside `render.py`'s `_legacy_overview`. `account_header` stays imported in the
CLI for error blocks.

- [ ] **Step 4: Run the targeted test, then the full refresh suite**

Run: `uv run pytest tests/test_cli_refresh.py -v`
Expected: PASS — the grouped test and all pre-existing refresh tests. The
`_fetch_and_render(...) is True/False` assertions remain untouched.

- [ ] **Step 5: Run the whole suite, lint, type-check, commit**

```bash
uv run pytest
uv run ruff format src/sidekick_usages/cli.py tests/test_cli_refresh.py
uv run ruff check src/sidekick_usages/cli.py tests/test_cli_refresh.py
uv run ty check
git add src/sidekick_usages/cli.py tests/test_cli_refresh.py
git commit -m "feat(cli): render check as grouped provider overview

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Add the explicit `set-plan` command

**Files:**
- Modify: `src/sidekick_usages/cli.py` (add a command near the other
  `@app.command(...)` definitions)
- Test: `tests/test_cli_setplan.py` (create)

**Interfaces:**
- Consumes: `AppContext`, `AccountStore`.
- Produces: the `set-plan` CLI command.

Context: the usage API cannot introspect an inference-only token's plan because
the header probe carries no tier and the token lacks `user:profile`. A manual
override is the correct primitive. Verify it only with an isolated test store
and keep its commit separate from rendering.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_setplan.py
"""set-plan command tests."""

import io
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.http import HttpClient
from sidekick_usages.store import Account, AccountStore


def _ctx(tmp_path: Path, account: Account) -> AccountStore:
    store = AccountStore(tmp_path / "accounts.json")
    store.upsert(account)
    store.save()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={},
            console=Console(file=io.StringIO(), force_terminal=False),
            err_console=Console(file=io.StringIO(), force_terminal=False),
        )
    )
    return store


def _acct(label: str, plan: str) -> Account:
    return Account(
        label=label, provider_id="claude", access_token="t", plan=plan
    )


def test_set_plan_updates_and_persists(tmp_path):
    store = _ctx(tmp_path, _acct("acme", "unknown"))

    result = CliRunner().invoke(cli.app, ["set-plan", "acme", "max"])

    assert result.exit_code == 0
    saved = AccountStore(tmp_path / "accounts.json").load().get("acme")
    assert saved is not None
    assert saved.plan == "max"


def test_set_plan_unknown_label_errors(tmp_path):
    _ctx(tmp_path, _acct("acme", "team"))

    result = CliRunner().invoke(cli.app, ["set-plan", "nope", "max"])

    assert result.exit_code == 1


def test_set_plan_rejects_empty_plan(tmp_path):
    _ctx(tmp_path, _acct("acme", "team"))

    result = CliRunner().invoke(cli.app, ["set-plan", "acme", ""])

    assert result.exit_code == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_setplan.py -v`
Expected: FAIL (no `set-plan` command → exit code 2 / usage error).

- [ ] **Step 3: Implement the command**

Add to `cli.py` (place beside `rename`/`remove` commands, ~line 748):

```python
@app.command("set-plan")
def set_plan_cmd(label: str, plan: str) -> None:
    """Manually set an account's plan tag.

    For credentials the usage API cannot introspect (e.g. inference-
    only Claude tokens), this is the supported way to correct the
    plan chip.
    """
    app_ctx = _get_ctx()
    value = plan.strip().lower()
    if not value:
        app_ctx.err_console.print("[red]Plan must not be empty.[/red]")
        raise typer.Exit(code=1)
    acct = app_ctx.store.get(label)
    if acct is None:
        app_ctx.err_console.print(
            f"[red]No account labeled '{label}'.[/red]"
        )
        raise typer.Exit(code=1)
    acct.plan = value
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
    app_ctx.console.print(f"Set [bold]{label}[/bold] plan to [bold]{value}[/bold].")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_setplan.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/sidekick_usages/cli.py tests/test_cli_setplan.py
uv run ruff check src/sidekick_usages/cli.py tests/test_cli_setplan.py
uv run ty check
git add src/sidekick_usages/cli.py tests/test_cli_setplan.py
git commit -m "feat(cli): add set-plan command for manual plan overrides

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Final verification (after all tasks)

```bash
uv run pytest                 # full suite green
uv run ruff check             # clean
uv run ty check               # clean
uv run pytest tests/test_render.py -k \
  "worst_case or degrades_below_floor or failure" -v
```

Confirm through the test-owned 85-column renderer fixture that nothing wraps
and `long.account.name@example.test` stays on one line. This is the exact
failure mode pinned by the width guard and does not touch live account state.
