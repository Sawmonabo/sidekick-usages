"""Rich-based rendering for per-account usage reports.

Returns :class:`rich.console.RenderableType` values so callers can
nest them in panels, tables, or print them directly. The braille
progress-bar aesthetic from cc-usage.py is preserved as a custom
renderable — Rich's stock :class:`rich.progress.BarColumn` uses
rectangular blocks which look bulky for this multi-line layout.
"""

import re
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from sidekick_usages.lifetime import format_since, format_tokens
from sidekick_usages.report import UsageReport, UsageWindow
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
        util_row: list[Text] = [
            _dot(provider_id),
            Text(acct.label, style="grey85"),
            _plan_text(acct),
        ]
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
    for label, sample in (
        ("<40", 20),
        ("40-69", 55),
        ("70-89", 80),
        ("≥90", 95),
    ):
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
    parts: list[RenderableType] = [
        _top_strip(len(pairs), len(order)),
        Text(""),
    ]
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
