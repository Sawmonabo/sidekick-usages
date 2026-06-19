"""Rich-based rendering for per-account usage reports.

Returns :class:`rich.console.RenderableType` values so callers can
nest them in panels, tables, or print them directly. The braille
progress-bar aesthetic from cc-usage.py is preserved as a custom
renderable — Rich's stock :class:`rich.progress.BarColumn` uses
rectangular blocks which look bulky for this multi-line layout.
"""

from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from sidekick_usages.report import UsageReport
from sidekick_usages.store import Account

BAR_WIDTH = 18

#: Utilization percentage thresholds for bar/percent coloring.
#: Values are the lower bound (inclusive) for each color band.
_PCT_RED_THRESHOLD = 90
_PCT_YELLOW_THRESHOLD = 70
_PCT_CYAN_THRESHOLD = 40

#: Seconds in common time units, used to choose the
#: ``in Xm`` / ``in Xh Xm`` / ``in Xd Xh`` rendering style.
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

#: Plan tag colors. Keyed by lowercased plan string.
PLAN_COLORS: dict[str, str] = {
    "max": "magenta",
    "team": "cyan",
    "pro": "green",
    "plus": "green",
    "enterprise": "yellow",
    "business": "yellow",
}

#: Provider tag colors.
PROVIDER_COLORS: dict[str, str] = {
    "claude": "magenta",
    "codex": "cyan",
}

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


def _utilization_color(pct: float) -> str:
    """Pick a Rich color name based on a utilization percentage.

    :param pct: Utilization 0-100.
    :return: Rich color name suitable for ``[color]text[/color]``.
    """
    if pct >= _PCT_RED_THRESHOLD:
        return "red"
    if pct >= _PCT_YELLOW_THRESHOLD:
        return "yellow"
    if pct >= _PCT_CYAN_THRESHOLD:
        return "cyan"
    return "green"


def _braille_bar(pct: float, width: int = BAR_WIDTH) -> Text:
    """Build a braille-dot progress bar as a Rich :class:`Text`.

    :param pct: 0-100; clamped if out of range.
    :param width: Total bar width in characters.
    :return: A Rich ``Text`` with two styled spans (filled, empty).
    """
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100.0 * width)
    empty = width - filled
    color = _utilization_color(pct)
    bar = Text()
    bar.append("⣿" * filled, style=color)
    bar.append("⣀" * empty, style="dim")
    return bar


def _format_reset(iso: str | None) -> Text:
    """Render a reset timestamp as ``<local> (<relative>)``.

    :param iso: ISO-8601 timestamp from the API, possibly ``None``.
    :return: A dim Rich ``Text`` (or em-dash for missing data).
    """
    if not iso:
        return Text("—", style="dim")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return Text(iso, style="dim")
    secs = int((dt - datetime.now(UTC)).total_seconds())
    if secs <= 0:
        rel = "any moment"
    elif secs < _SECONDS_PER_HOUR:
        rel = f"in {secs // 60}m"
    elif secs < _SECONDS_PER_DAY:
        h, m = divmod(secs // 60, 60)
        rel = f"in {h}h {m}m"
    else:
        d, rem = divmod(secs, _SECONDS_PER_DAY)
        rel = f"in {d}d {rem // _SECONDS_PER_HOUR}h"
    local = dt.astimezone()
    return Text(
        f"↻ {local.strftime('%a %b %d, %I:%M %p')} ({rel})",
        style="dim",
    )


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
    secs = round((dt - datetime.now(UTC)).total_seconds())
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


def _account_tag(acct: Account) -> Text:
    """Build the ``[provider · plan]`` colored tag.

    :param acct: Account whose provider and plan to show.
    :return: A Rich ``Text`` ready for direct printing.
    """
    prov_color = PROVIDER_COLORS.get(acct.provider_id, "dim")
    plan_color = PLAN_COLORS.get(acct.plan, "dim")
    tag = Text()
    if not acct.plan or acct.plan == "unknown":
        tag.append("[", style="dim")
        tag.append(acct.provider_id, style=prov_color)
        tag.append("]", style="dim")
        return tag
    tag.append("[", style="dim")
    tag.append(acct.provider_id, style=prov_color)
    tag.append(" · ", style="dim")
    tag.append(acct.plan, style=plan_color)
    tag.append("]", style="dim")
    return tag


def account_header(acct: Account) -> Text:
    """Render a standalone header line.

    Used by error blocks where there's no report to align against.

    :param acct: Account to display.
    :return: A Rich ``Text`` of ``label  [provider · plan]``.
    """
    header = Text()
    header.append(acct.label, style="bold")
    header.append("  ")
    header.append_text(_account_tag(acct))
    return header


def usage_report(
    acct: Account,
    report: UsageReport,
) -> RenderableType:
    """Render the full per-account block.

    Layout: a header line with the account label and tag, followed
    by a borderless table of one row per active window. Columns:
    name, bar, percent, reset time.

    :param acct: Account being reported on.
    :param report: Parsed usage data.
    :return: A Rich ``Group`` ready to print or nest in a panel.
    """
    windows = report.active_windows()
    if not windows:
        return Group(
            account_header(acct),
            Text(
                "  No active usage windows reported.",
                style="dim",
            ),
        )

    table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column("name", style="dim", no_wrap=True)
    table.add_column("bar", no_wrap=True)
    table.add_column("pct", justify="right", no_wrap=True)
    table.add_column("reset", no_wrap=True)

    for w in windows:
        pct_int = round(w.utilization)
        pct_text = Text(
            f"{pct_int}%",
            style=_utilization_color(w.utilization),
        )
        table.add_row(
            f" {w.name}",
            _braille_bar(w.utilization),
            pct_text,
            _format_reset(w.resets_at),
        )

    return Group(account_header(acct), table)
