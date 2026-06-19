import io
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from sidekick_usages import render
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


def test_heat_tile_zero_is_centered_dot_no_fill():
    tile = render._heat_tile(0)
    assert tile.plain == f"{'·':^{render._TILE_WIDTH}}"
    assert tile.style == render._IDLE_FG


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
                ("Spark 5h", 0, iso),
                ("Spark 7d", 0, iso),
            ),
        ),
        (
            _acct("sabossedgh@fortressinfosec.com", "codex", "pro"),
            _report(
                ("5h", 0, iso),
                ("7d", 0, iso),
                ("Spark 5h", 0, iso),
                ("Spark 7d", 0, iso),
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


def test_width_guard_fits_80_columns():
    out = _render_at(80, _worst_case_pairs())
    lines = out.split("\n")
    assert max(len(line) for line in lines) <= 80  # noqa: PLR2004
    # longest name intact on a single physical line, not elided
    assert any("sabossedgh@fortressinfosec.com" in line for line in lines)


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
    assert "a.sawmon@gmail" in out


def test_overview_empty_pairs():
    out = _render_at(80, [])
    assert "No usage" in out
