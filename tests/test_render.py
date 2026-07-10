import io
from datetime import timedelta

import pytest
from rich.console import Console

from sidekick_usages import render
from sidekick_usages.render import FetchFailure
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account
from tests.test_support import REFERENCE_TIME


def _iso_after(**delta: float) -> str:
    return (REFERENCE_TIME + timedelta(**delta)).isoformat()


def test_heat_band_picks_inclusive_lower_bounds():
    assert render._heat_band(90) == ("#ffe6e6", "#b03030")
    assert render._heat_band(89) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(70) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(40) == ("#e2fbff", "#1b6a87")
    assert render._heat_band(1) == ("#dfffe9", "#1d5e35")
    assert render._heat_band(0) is None


def test_heat_tile_zero_is_grey_filled_percent():
    tile = render._heat_tile(0)
    assert tile.plain == f"{'0%':^{render._TILE_WIDTH}}"
    assert tile.style == f"{render._ZERO_FG} on {render._ZERO_BG}"


def test_heat_tile_nonzero_is_centered_percent_on_band():
    tile = render._heat_tile(94)
    assert tile.plain == f"{'94%':^{render._TILE_WIDTH}}"
    assert tile.style == "#ffe6e6 on #b03030"


def test_format_reset_compact_buckets():
    assert render._format_reset_compact(None, REFERENCE_TIME) == ""
    assert render._format_reset_compact("not-a-date", REFERENCE_TIME) == ""
    assert (
        render._format_reset_compact(
            _iso_after(minutes=-5),
            REFERENCE_TIME,
        )
        == "now"
    )
    assert (
        render._format_reset_compact(
            _iso_after(minutes=45),
            REFERENCE_TIME,
        )
        == "45m"
    )
    assert (
        render._format_reset_compact(
            _iso_after(hours=3, minutes=50),
            REFERENCE_TIME,
        )
        == "3h 50m"
    )
    assert (
        render._format_reset_compact(
            _iso_after(days=1, hours=15),
            REFERENCE_TIME,
        )
        == "1d 15h"
    )


def test_reset_cell_is_centered_dim():
    cell = render._reset_cell(
        _iso_after(hours=3, minutes=50),
        REFERENCE_TIME,
    )
    assert cell.plain == f"{'3h 50m':^{render._TILE_WIDTH}}"
    assert cell.style == "grey42"
    assert render._reset_cell(None, REFERENCE_TIME).plain == (
        f"{'':^{render._TILE_WIDTH}}"
    )


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
    assert render._length_hours("5h") < render._length_hours("7d")


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
    # 3 Claude + 2 Codex; the reserved 30-char name + Spark block is the
    # binding width case.
    iso = _iso_after(hours=3, minutes=50)
    claude = [
        (
            _acct("short.account@example.test"),
            _report(("5h", 94, iso), ("7d", 61, iso)),
        ),
        (
            _acct("team.account@example.test", plan="team"),
            _report(("5h", 12, iso), ("7d", 73, iso)),
        ),
        (
            _acct("third.account@example.test"),
            _report(("5h", 40, iso), ("7d", 5, iso)),
        ),
    ]
    codex = [
        (
            _acct("codex@example.test", "codex", "pro"),
            _report(
                ("5h", 8, iso),
                ("7d", 45, iso),
                ("GPT-5.3-Codex-Spark 5h", 0, iso),
                ("GPT-5.3-Codex-Spark 7d", 0, iso),
            ),
        ),
        (
            _acct("long.account.name@example.test", "codex", "pro"),
            _report(
                ("5h", 0, iso),
                ("7d", 0, iso),
                ("GPT-5.3-Codex-Spark 5h", 0, iso),
                ("GPT-5.3-Codex-Spark 7d", 0, iso),
            ),
        ),
    ]
    return claude + codex


_LIFETIME: dict[str, tuple[int, str | None]] = {
    "claude": (424_000_000, "2025-12-28"),
    "codex": (212_000_000, "2026-03-30"),
}

#: The documented panel floor (spec §8/§10). The Framed-Panels redesign
#: must render as real panels — not the legacy fallback — for the worst-case
#: store at this width. If a change pushes the binding panel width past it,
#: the floor guard below fails instead of the layout silently degrading.
_PANEL_FLOOR = 85


def _render_at(
    width: int,
    pairs: list[tuple[Account, UsageReport]],
    *,
    failures: list[tuple[Account, FetchFailure]] | None = None,
) -> str:
    buf = io.StringIO()
    # legacy_windows=False keeps Rich's rounded box on every platform.
    # Without it, Windows CI substitutes the square box (┌ for ╭) and the
    # panel-corner assertions below fail.
    console = Console(width=width, file=buf, legacy_windows=False)
    console.print(
        render.usage_overview(
            pairs,
            _LIFETIME,
            failures=failures,
            width=width,
            reference_time=REFERENCE_TIME,
        )
    )
    return buf.getvalue()


@pytest.mark.parametrize(
    ("width", "expected"),
    [(200, "3h 50m"), (70, "(in 3h 50m)")],
)
def test_overview_uses_explicit_reference_time(
    width: int,
    expected: str,
) -> None:
    pairs = [
        (
            _acct("fixed-time"),
            _report(
                (
                    "5h",
                    1,
                    _iso_after(hours=3, minutes=50),
                )
            ),
        )
    ]

    assert expected in _render_at(width, pairs)


def _panel_line_widths(out: str) -> set[int]:
    # ``line and`` guards the blank separator lines: "" is a substring
    # of every string, so "" in "╭│╰" is True and would count width 0.
    return {
        len(line) for line in out.split("\n") if line and line[:1] in "╭│╰"
    }


def test_panels_share_one_width():
    # measure-then-pin: every provider panel is pinned to the single
    # binding width, so all panel border/interior lines share one right edge.
    out = _render_at(200, _worst_case_pairs())
    widths = _panel_line_widths(out)
    assert len(widths) == 1


def test_worst_case_renders_as_panels_at_floor():
    # The reserved worst-case fixture (30-char name + Spark block) must render
    # as framed panels at the documented floor, with nothing wrapping past
    # the frame and the longest account name intact on one row. Fails if the
    # binding width grows past the floor (a real regression); still passes if
    # the layout gets tighter (an improvement must not break the guard).
    out = _render_at(_PANEL_FLOOR, _worst_case_pairs())
    assert "╭─ CLAUDE · 3 accounts ─" in out  # panel path, not legacy
    assert "╭─ CODEX · 2 accounts ─" in out
    assert max(len(line) for line in out.split("\n")) <= _PANEL_FLOOR
    assert "long.account.name@example.test" in out


def test_overview_shows_robot_masthead_titles_and_lifetime():
    out = _render_at(120, _worst_case_pairs())
    assert "      o" in out
    assert "     .-." in out
    assert "  .--┴-┴--.    sidekick usages" in out
    assert (
        "  | O   O |   >> A multi-account usage dashboard for "
        "Claude Code and Codex CLI."
    ) in out
    assert (
        "  | ||||| |   >> Limits + resets + account status, one terminal."
        in out
    )
    assert "  '--___--'" in out
    assert "5 accounts · 2 providers" not in out
    assert "╭─ CLAUDE · 3 accounts ─" in out
    assert "╭─ CODEX · 2 accounts ─" in out
    assert "424M output" in out
    assert "since Mar 30" in out
    assert "GPT-5.3-Codex-Spark" in out


def test_provider_title_uses_singular_account_count():
    pairs = [
        (
            _acct("only", "codex", "pro"),
            _report(
                ("5h", 5, _iso_after(hours=1)),
                ("7d", 9, _iso_after(days=1)),
            ),
        )
    ]
    out = _render_at(120, pairs)
    assert "╭─ CODEX · 1 account ─" in out
    assert "CODEX · 1 accounts" not in out


def test_overview_degrades_below_floor_to_legacy():
    # Well below the binding panel width the renderer falls back to the
    # legacy stacked view instead of squeezing/wrapping the panels.
    # Discriminator: the uppercase panel title only exists on the panel
    # path; the legacy tag uses the lowercase provider id. Branding keeps the
    # complete robot but drops the wide product copy.
    out = _render_at(70, _worst_case_pairs())
    assert "CLAUDE" not in out
    assert ".--┴-┴--.  sidekick usages" in out
    assert "A multi-account usage dashboard" not in out
    assert "long.account.name@example.test" in out


def test_overview_empty_pairs():
    out = _render_at(80, [])
    assert "No usage" in out


def test_named_group_caption_row_and_rule_present():
    out = _render_at(200, _worst_case_pairs())
    cap = next(
        line for line in out.split("\n") if "GPT-5.3-Codex-Spark" in line
    )
    assert "%" not in cap  # caption sits above the tiles, not inline
    assert "│" in out  # the model rule is drawn on data rows


def test_subtitle_not_truncated_when_wider_than_content():
    pairs = [
        (
            _acct("x", "codex", "pro"),
            _report(
                ("5h", 5, _iso_after(hours=1)),
                ("7d", 9, _iso_after(days=1)),
            ),
        )
    ]
    # Annotated, not inferred: dict is invariant in its value type, so a
    # str-only literal won't assign to the str | None parameter.
    lifetime: dict[str, tuple[int, str | None]] = {
        "codex": (999_000_000, "2024-01-01")
    }
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        render.usage_overview(
            pairs,
            lifetime,
            width=200,
            reference_time=REFERENCE_TIME,
        )
    )
    out = buf.getvalue()
    assert "…" not in out
    assert "999M output" in out


def test_failure_renders_in_provider_panel():
    iso = _iso_after(hours=3)
    pairs = [
        (
            _acct("acct-ok", "codex", "pro"),
            _report(("5h", 8, iso), ("7d", 45, iso)),
        )
    ]
    failures = [
        (
            _acct("long.account.name@example.test", "codex", "pro"),
            FetchFailure(
                "token expired",
                (
                    "Log in to Codex CLI again, then run:",
                    "sidekick-usages refresh long.account.name@example.test",
                ),
            ),
        )
    ]
    out = _render_at(200, pairs, failures=failures)
    assert "⚠ token expired" in out
    assert "Log in to Codex CLI again, then run:" in out
    assert "sidekick-usages refresh long.account.name@example.test" in out
    assert "╭─ CODEX · 2 accounts ─" in out
    assert "needs attention" not in out
    first = next(line for line in out.splitlines() if line.strip())
    assert first.strip() == "o"


def test_all_failed_provider_has_no_orphan_header():
    failures = [
        (
            _acct("long.account.name@example.test", "codex", "pro"),
            FetchFailure("token expired", ("retry later",)),
        )
    ]
    out = _render_at(200, [], failures=failures)
    assert "⚠ token expired" in out
    assert "╭─ CODEX · 1 account ─" in out
    assert "needs attention" not in out
    assert "5h" not in out  # no orphan matrix header


def test_failures_widen_shared_panels():
    iso = _iso_after(hours=3)
    pairs = [
        (
            _acct("short.account@example.test", "claude", "max"),
            _report(("5h", 94, iso), ("7d", 61, iso)),
        )
    ]
    failures = [
        (
            _acct("long.account.name@example.test", "codex", "pro"),
            FetchFailure(
                "token expired",
                (
                    "Log in to Codex CLI again, then run:",
                    "sidekick-usages refresh long.account.name@example.test",
                ),
            ),
        )
    ]
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        render.usage_overview(
            pairs,
            _LIFETIME,
            failures=failures,
            width=200,
            reference_time=REFERENCE_TIME,
        )
    )
    out = buf.getvalue()
    widths = _panel_line_widths(out)
    assert len(widths) == 1
    assert "sidekick-usages refresh long.account.name@example.test" in out


def test_legacy_mode_renders_failures():
    iso = _iso_after(hours=3)
    pairs = [
        (
            _acct("acct-ok", "codex", "pro"),
            _report(("5h", 8, iso), ("7d", 45, iso)),
        )
    ]
    failures = [
        (
            _acct("long.account.name@example.test", "codex", "pro"),
            FetchFailure("token expired", ("retry later",)),
        )
    ]
    buf = io.StringIO()
    console = Console(width=40, file=buf)
    console.print(
        render.usage_overview(
            pairs,
            _LIFETIME,
            failures=failures,
            width=40,
            reference_time=REFERENCE_TIME,
        )
    )
    out = buf.getvalue()
    assert "token expired" in out


def test_panels_have_interior_top_padding():
    out = _render_at(200, _worst_case_pairs())
    lines = out.splitlines()
    tops = [i for i, line in enumerate(lines) if line.lstrip().startswith("╭")]
    assert tops  # at least one panel
    for i in tops:
        inner = lines[i + 1]
        # the row right under the top border is blank between the borders
        assert inner.lstrip().startswith("│")
        assert inner.strip("│ ") == ""


def test_named_panel_separates_caption_from_header():
    out = _render_at(200, _worst_case_pairs())
    lines = out.splitlines()
    cap = next(
        i for i, line in enumerate(lines) if "GPT-5.3-Codex-Spark" in line
    )
    assert lines[cap + 1].strip("│ ") == ""  # blank separator
    assert "5h" in lines[cap + 2]  # header follows
