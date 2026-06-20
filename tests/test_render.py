import io
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from sidekick_usages import render
from sidekick_usages.render import FetchFailure
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account


def _iso_in(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


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
    assert render._format_reset_compact(None) == ""
    assert render._format_reset_compact("not-a-date") == ""
    assert render._format_reset_compact(_iso_in(minutes=-5)) == "now"
    assert render._format_reset_compact(_iso_in(minutes=45)) == "45m"
    assert (
        render._format_reset_compact(_iso_in(hours=3, minutes=50)) == "3h 50m"
    )
    assert render._format_reset_compact(_iso_in(days=1, hours=15)) == "1d 15h"


def test_format_reset_compact_tz_naive_does_not_crash():
    assert render._format_reset_compact("2026-06-19T12:00:00") == ""


def test_format_reset_tz_naive_does_not_crash():
    out = render._format_reset("2026-06-19T12:00:00")
    # tz-naive -> swallowed -> passthrough dim text, not a crash
    assert "2026-06-19T12:00:00" in out.plain


def test_reset_cell_is_centered_dim():
    cell = render._reset_cell(_iso_in(hours=3, minutes=50))
    assert cell.plain == f"{'3h 50m':^{render._TILE_WIDTH}}"
    assert cell.style == "grey42"
    assert render._reset_cell(None).plain == f"{'':^{render._TILE_WIDTH}}"


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
    assert render._length_hours("5h") == 5  # noqa: PLR2004
    assert render._length_hours("7d") == 168  # noqa: PLR2004
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
    # 3 Claude + 2 Codex; the 30-char Codex name + Spark block is the
    # binding width case (matches the user's real store).
    iso = _iso_in(hours=3, minutes=50)
    claude = [
        (
            _acct("SAbossedgh@fortressinfosec"),
            _report(("5h", 94, iso), ("7d", 61, iso)),
        ),
        (
            _acct("SAbossedgh@fortressinfosec@org", plan="team"),
            _report(("5h", 12, iso), ("7d", 73, iso)),
        ),
        (_acct("a.sawmon@gmail"), _report(("5h", 40, iso), ("7d", 5, iso))),
    ]
    codex = [
        (
            _acct("a.sawmon@ymail.com", "codex", "pro"),
            _report(
                ("5h", 8, iso),
                ("7d", 45, iso),
                ("GPT-5.3-Codex-Spark 5h", 0, iso),
                ("GPT-5.3-Codex-Spark 7d", 0, iso),
            ),
        ),
        (
            _acct("sabossedgh@fortressinfosec.com", "codex", "pro"),
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


def _render_at(width: int, pairs: list[tuple[Account, UsageReport]]) -> str:
    buf = io.StringIO()
    console = Console(width=width, file=buf)
    console.print(render.usage_overview(pairs, _LIFETIME, width=width))
    return buf.getvalue()


def _panel_line_widths(out: str) -> set[int]:
    # ``line and`` guards the blank separator lines: "" is a substring
    # of every string, so "" in "╭│╰" is True and would count width 0.
    return {
        len(line) for line in out.split("\n") if line and line[:1] in "╭│╰"
    }


def test_panels_share_one_width_and_guard_at_boundary():
    pairs = _worst_case_pairs()
    wide = _render_at(200, pairs)  # panels pin to natural required < 200
    widths = _panel_line_widths(wide)
    assert len(widths) == 1  # equal width, one shared right edge
    required = widths.pop()
    longest = max(len(line) for line in wide.split("\n"))
    assert longest <= required  # nothing overflows the shared width
    at = _render_at(required, pairs)  # exactly required -> still panels
    assert "CLAUDE" in at
    assert "CODEX" in at
    narrower = _render_at(required - 1, pairs)  # one col short -> legacy
    assert "CLAUDE" not in narrower
    assert "a.sawmon@gmail" in narrower


def test_overview_shows_titles_and_lifetime():
    out = _render_at(120, _worst_case_pairs())
    assert "CLAUDE" in out
    assert "CODEX" in out
    assert "424M output" in out
    assert "since Mar 30" in out
    assert "GPT-5.3-Codex-Spark" in out


def test_overview_degrades_below_80_to_legacy():
    # Below the binding panel width the renderer falls back to the
    # legacy stacked view instead of squeezing/wrapping the panels.
    # Discriminator: the uppercase panel title only exists on the
    # panel path; the legacy tag uses the lowercase provider id.
    out = _render_at(70, _worst_case_pairs())
    assert "CLAUDE" not in out
    assert "a.sawmon@gmail" in out


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
                ("5h", 5, _iso_in(hours=1)),
                ("7d", 9, _iso_in(days=1)),
            ),
        )
    ]
    lifetime = {"codex": (999_000_000, "2024-01-01")}
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(render.usage_overview(pairs, lifetime, width=200))
    out = buf.getvalue()
    assert "…" not in out
    assert "999M output" in out


def test_failure_renders_in_provider_panel():
    iso = _iso_in(hours=3)
    pairs = [
        (
            _acct("acct-ok", "codex", "pro"),
            _report(("5h", 8, iso), ("7d", 45, iso)),
        )
    ]
    failures = [
        (
            _acct("a.sawmon@ymail.com", "codex", "pro"),
            FetchFailure(
                "token expired",
                (
                    "Log in to Codex CLI again, then run:",
                    "sidekick-usages refresh a.sawmon@ymail.com",
                ),
            ),
        )
    ]
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        render.usage_overview(pairs, _LIFETIME, failures=failures, width=200)
    )
    out = buf.getvalue()
    assert "⚠ token expired" in out
    assert "Log in to Codex CLI again, then run:" in out
    assert "sidekick-usages refresh a.sawmon@ymail.com" in out
    first = next(line for line in out.splitlines() if line.strip())
    assert first.lstrip().startswith("sidekick")


def test_all_failed_provider_has_no_orphan_header():
    failures = [
        (
            _acct("a.sawmon@ymail.com", "codex", "pro"),
            FetchFailure("token expired", ("retry later",)),
        )
    ]
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        render.usage_overview([], _LIFETIME, failures=failures, width=200)
    )
    out = buf.getvalue()
    assert "⚠ token expired" in out
    assert "5h" not in out  # no orphan matrix header


def test_failures_widen_shared_panels():
    iso = _iso_in(hours=3)
    pairs = [
        (
            _acct("SAbossedgh@fortressinfosec", "claude", "max"),
            _report(("5h", 94, iso), ("7d", 61, iso)),
        )
    ]
    failures = [
        (
            _acct("sabossedgh@fortressinfosec.com", "codex", "pro"),
            FetchFailure(
                "token expired",
                (
                    "Log in to Codex CLI again, then run:",
                    "sidekick-usages refresh sabossedgh@fortressinfosec.com",
                ),
            ),
        )
    ]
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        render.usage_overview(pairs, _LIFETIME, failures=failures, width=200)
    )
    out = buf.getvalue()
    widths = _panel_line_widths(out)
    assert len(widths) == 1
    assert "sidekick-usages refresh sabossedgh@fortressinfosec.com" in out


def test_failures_default_keeps_existing_render():
    out = _render_at(200, _worst_case_pairs())
    assert "CLAUDE" in out
    assert "CODEX" in out


def test_legacy_mode_renders_failures():
    iso = _iso_in(hours=3)
    pairs = [
        (
            _acct("acct-ok", "codex", "pro"),
            _report(("5h", 8, iso), ("7d", 45, iso)),
        )
    ]
    failures = [
        (
            _acct("a.sawmon@ymail.com", "codex", "pro"),
            FetchFailure("token expired", ("retry later",)),
        )
    ]
    buf = io.StringIO()
    console = Console(width=40, file=buf)
    console.print(
        render.usage_overview(pairs, _LIFETIME, failures=failures, width=40)
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
