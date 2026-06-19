from datetime import UTC, datetime, timedelta

from sidekick_usages import render


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
