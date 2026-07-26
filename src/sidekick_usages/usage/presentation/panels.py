"""Shared provider panel construction."""

import re
from collections.abc import Sequence
from datetime import datetime

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.models import UsageReport, UsageWindow
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardProvider,
)
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    MetricsFreshness,
    ProviderTokenActivity,
    TokenActivityIssue,
)
from sidekick_usages.usage.presentation.activity import (
    activity_failure_label,
    panel_activity_text,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    PLAN_COLORS,
    account_activity_issues,
    account_dot,
    activity_issue_copy,
    cursor_account_dot,
    failure_copy,
    row_details,
    row_label,
    row_plan,
)
from sidekick_usages.usage.presentation.reset import compact_reset_text

#: Heat bands as (lower-bound-inclusive percent, fg hex, bg hex).
#: Thresholds match the narrow renderer's ``_utilization_color`` bands.
_HEAT_BANDS: list[tuple[int, str, str]] = [
    (90, "#ffe6e6", "#b03030"),
    (70, "#fff4e0", "#9c6f12"),
    (40, "#e2fbff", "#1b6a87"),
    (1, "#dfffe9", "#1d5e35"),
]

_IDLE_FG = "grey39"

_ZERO_FG = "#cdd3d8"
_ZERO_BG = "#353a40"

_TILE_WIDTH = 6

#: Color of the ``│`` rule separating primary from a named-group column.
_RULE_STYLE = "#356f78"

#: Corners + dashes + spaces framing a title/subtitle on the panel border.
#: ``Panel`` measurement ignores the subtitle, so the panel's minimum width
#: is floored by ``len(label) + _PANEL_CHROME`` to keep it from truncating.
_PANEL_CHROME = 6

#: Matches a window length token such as ``5h`` or ``7d``.
_LENGTH_RE = re.compile(r"\d+[hd]")


def _classify_window(name: str) -> tuple[str, str]:
    """Split a window name into its detected length and optional group."""
    match = _LENGTH_RE.search(name)
    if match is None:
        return (name.strip(), "")
    length = match.group(0)
    group = (name[: match.start()] + name[match.end() :]).strip()
    return (length, group)


def _length_hours(length: str) -> int:
    """Return an hour-based sort key for one length token."""
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
    :return: A ``Text`` of width ``_TILE_WIDTH``: a neutral-grey
        centered ``0%`` at 0, otherwise ``NN%`` centered on the band
        color.
    """
    band = _heat_band(pct)
    if band is None:
        return Text(
            f"{'0%':^{_TILE_WIDTH}}", style=f"{_ZERO_FG} on {_ZERO_BG}"
        )
    fg, bg = band
    return Text(f"{f'{pct}%':^{_TILE_WIDTH}}", style=f"{fg} on {bg}")


def _reset_cell(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Build one fixed-width, dim, centered reset-countdown cell.

    :param reset_at: Aware provider-normalized reset time, if known.
    :param reference_time: Aware wall time for relative formatting.
    :return: A ``Text`` of width ``_TILE_WIDTH``.
    """
    return Text(
        f"{compact_reset_text(reset_at, reference_time):^{_TILE_WIDTH}}",
        style="grey42",
    )


def _plan_text(plan: str) -> Text:
    """Plan chip, suppressed for empty/unknown (matches narrow tag)."""
    if not plan or plan == "unknown":
        return Text("")
    return Text(plan, style=PLAN_COLORS.get(plan, "grey42"))


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


def _reset_or_blank(
    window: UsageWindow | None,
    reference_time: datetime,
) -> Text:
    if window is None:
        return Text("")
    return _reset_cell(window.resets_at, reference_time)


def _rule_cell() -> Text:
    """The ``│`` separating the primary tiles from a named-group column."""
    return Text("│", style=_RULE_STYLE)


def _model_width(name: str, n_lengths: int) -> int:
    """Width of a named group's MODEL column.

    Wide enough for either the full model name or its length tiles
    (so a long name like ``GPT-5.3-Codex-Spark`` is never truncated;
    Rich has no colspan, so the name lives in one wide column).

    :param name: The group/model label shown as the caption.
    :param n_lengths: How many length tiles sit under the caption.
    :return: ``max(len(name), tiles)`` in cells.
    """
    tiles = _TILE_WIDTH * n_lengths + 2 * (n_lengths - 1)
    return max(len(name), tiles)


def _model_subgrid(cells: list[Text]) -> Table:
    """Lay out one named group's length cells inside a MODEL cell.

    :param cells: One ``Text`` per length (label, tile, or reset).
    :return: A borderless ``Table.grid`` of centered ``_TILE_WIDTH``
        columns matching the primary tile spacing.
    """
    grid = Table.grid(padding=(0, 1))
    for _ in cells:
        grid.add_column(width=_TILE_WIDTH, justify="center")
    grid.add_row(*cells)
    return grid


def _build_table(
    namew: int,
    primary: list[str],
    named: list[tuple[str, list[str]]],
    *,
    cursor: bool = False,
) -> Table:
    table = Table(
        box=None,
        show_header=False,  # header is added manually as a styled row
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column(width=3 if cursor else 1)  # cursor + dot, or dot
    table.add_column(width=namew)  # name
    table.add_column(width=4)  # plan
    for _length in primary:
        table.add_column(width=_TILE_WIDTH, justify="center")
    for group, lengths in named:
        table.add_column(width=1, justify="center")  # rule
        table.add_column(
            width=_model_width(group, len(lengths)), justify="left"
        )  # MODEL (wide)
    blank = Text("")
    if named:
        # Caption row: the model name sits above its tiles; the rule
        # cell is blank so no ``│`` is drawn on this row.
        caption: list[Text] = [blank, blank, blank]
        caption.extend(blank for _ in primary)
        for group, _lengths in named:
            caption.append(blank)
            caption.append(Text(group, style="grey46"))
        table.add_row(*caption)
        # Blank line separating the model caption from the 5h/7d header.
        n_cols = 3 + len(primary) + 2 * len(named)
        table.add_row(*([blank] * n_cols))
    header: list[RenderableType] = [blank, blank, blank]
    header.extend(Text(length, style="grey42") for length in primary)
    for _group, lengths in named:
        header.append(_rule_cell())
        header.append(
            _model_subgrid(
                [Text(length, style="grey42") for length in lengths]
            )
        )
    table.add_row(*header)
    return table


def provider_panel(
    provider_id: ProviderId,
    usages: list[AccountUsage],
    failures: list[FetchFailure],
    namew: int,
    activity: ProviderTokenActivity | None,
    reference_time: datetime,
) -> Panel:
    """Build one provider panel for a one-shot usage result."""
    blocks: list[RenderableType] = []
    if usages:
        primary, named = _panel_columns([usage.report for usage in usages])
        n_cols = 3 + len(primary) + 2 * len(named)
        table = _build_table(namew, primary, named)
        for position, usage in enumerate(usages):
            if position:
                table.add_row(*([Text("")] * n_cols))
            index = _window_index(usage.report)
            util_row: list[RenderableType] = [
                account_dot(provider_id),
                Text(usage.label, style="grey85"),
                _plan_text(usage.plan),
            ]
            reset_row: list[RenderableType] = [Text(""), Text(""), Text("")]
            for length in primary:
                window = index.get(("", length))
                util_row.append(_util_cell(window))
                reset_row.append(_reset_or_blank(window, reference_time))
            for group, lengths in named:
                util_row.append(_rule_cell())
                util_row.append(
                    _model_subgrid(
                        [
                            _util_cell(index.get((group, length)))
                            for length in lengths
                        ]
                    )
                )
                reset_row.append(_rule_cell())
                reset_row.append(
                    _model_subgrid(
                        [
                            _reset_or_blank(
                                index.get((group, length)),
                                reference_time,
                            )
                            for length in lengths
                        ]
                    )
                )
            table.add_row(*util_row)
            table.add_row(*reset_row)
        blocks.append(table)
        blocks.extend(_stale_usage_lines(usages))
    if failures:
        if blocks:
            blocks.append(Text(""))  # gap between successes and failures
        blocks.append(_error_table(provider_id, failures, namew))
    activity_issues = account_activity_issues(activity)
    if activity_issues:
        if blocks:
            blocks.append(Text(""))
        blocks.append(
            _activity_issue_table(provider_id, activity_issues, namew)
        )
    content: RenderableType = blocks[0] if len(blocks) == 1 else Group(*blocks)
    color = PROVIDER_COLORS.get(provider_id, "white")
    account_count = len(
        {usage.label for usage in usages}
        | {failure.label for failure in failures}
    )
    account_noun = "account" if account_count == 1 else "accounts"
    title = Text()
    title.append(provider_id.upper(), style=f"bold {color}")
    title.append(
        f" · {account_count} {account_noun}",
        style="grey54",
    )
    subtitle = panel_activity_text(activity) if activity is not None else None
    return Panel(
        content,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=color,
        padding=(1, 2),
        expand=False,
    )


def _stale_usage_lines(usages: Sequence[AccountUsage]) -> tuple[Text, ...]:
    """Return visible timestamped warnings for retained usage rows."""
    return tuple(
        Text(
            f"⚠ {usage.label}: last known · {usage.fetched_at.isoformat()}",
            style="yellow",
        )
        for usage in usages
        if usage.freshness is MetricsFreshness.STALE
    )


def _error_table(
    provider_id: ProviderId,
    failures: list[FetchFailure],
    namew: int,
) -> Table:
    """Build account-aligned provider failure rows."""
    rows: list[tuple[str, str, Text, tuple[Text, ...]]] = []
    for failure in failures:
        status_label, detail_lines = failure_copy(failure)
        status = Text(f"⚠ {status_label}", style="yellow")
        detail = tuple(Text(line, style="grey54") for line in detail_lines)
        rows.append((failure.label, failure.plan, status, detail))
    return _warning_table(provider_id, rows, namew)


def _warning_table(
    provider_id: ProviderId,
    rows: list[tuple[str, str, Text, tuple[Text, ...]]],
    namew: int,
) -> Table:
    """Align account warnings with the shared dot, label, and plan columns."""
    rest_width = max(
        1,
        *(
            text.cell_len
            for _label, _plan, status, detail in rows
            for text in (status, *detail)
        ),
    )
    table = Table(
        box=None,
        show_header=False,
        padding=(0, 1),
        pad_edge=False,
    )
    for width in (1, namew, 4, rest_width):
        table.add_column(width=width)
    for position, (label, plan, status, detail) in enumerate(rows):
        if position:
            table.add_row(*([Text("")] * 4))
        table.add_row(
            account_dot(provider_id),
            Text(label, style="grey85"),
            _plan_text(plan),
            Group(status, *detail),
        )
    return table


def _activity_issue_table(
    provider_id: ProviderId,
    issues: tuple[TokenActivityIssue, ...],
    namew: int,
) -> Table:
    """Build account-aligned warning rows for activity read failures."""
    rows: list[tuple[str, str, Text, tuple[Text, ...]]] = []
    for issue in issues:
        if issue.label is None:
            raise ValueError("Account activity issue requires a label.")
        status = Text(
            f"⚠ {activity_failure_label(issue.kind)}",
            style="yellow",
        )
        detail = tuple(
            Text(line, style="grey54")
            for line in activity_issue_copy(provider_id, issue)
        )
        rows.append((issue.label, "unknown", status, detail))
    return _warning_table(provider_id, rows, namew)


def panel_min_width(measure: Console, panel: Panel) -> int:
    """Natural width of a panel, floored by its title/subtitle.

    ``Panel`` measurement ignores the title/subtitle text, so a long
    subtitle would otherwise be truncated once the panel is pinned to
    its content width. Floor the result by ``label + _PANEL_CHROME``
    for each border label so neither is clipped.

    :param measure: A wide throwaway ``Console`` used only to measure.
    :param panel: A panel built with ``expand=False`` (natural width).
    :return: The minimum width that fits content, title, and subtitle.
    """
    width = measure.measure(panel).maximum
    for label in (panel.title, panel.subtitle):
        if label is not None:
            width = max(width, measure.measure(label).maximum + _PANEL_CHROME)
    return width


def provider_order(
    usages: Sequence[AccountUsage],
    failures: Sequence[FetchFailure] = (),
) -> list[ProviderId]:
    """Return provider IDs in their first observed result order."""
    order: list[ProviderId] = []
    provider_ids = (
        *(usage.provider_id for usage in usages),
        *(failure.provider_id for failure in failures),
    )
    for provider_id in provider_ids:
        if provider_id not in order:
            order.append(provider_id)
    return order


def legend() -> Text:
    """Build the utilization heat legend."""
    legend = Text()
    for label, sample in (
        ("<40", 20),
        ("40-69", 55),
        ("70-89", 80),
        ("≥90", 95),
    ):
        band = _heat_band(sample)
        foreground, background = band if band else (_IDLE_FG, "default")
        legend.append(
            f" {label} ",
            style=f"{foreground} on {background}",
        )
        legend.append("  ")
    legend.append("   dim = resets in", style="grey42")
    return legend


def dashboard_provider_panel(
    provider: DashboardProvider,
    cursor: DashboardCursor,
    namew: int,
    activity: ProviderTokenActivity | None,
    reference_time: datetime,
) -> Panel:
    """Build one provider panel for the interactive dashboard."""
    reports = [
        row.usage.report
        for row in provider.rows
        if isinstance(row, DashboardAccount) and row.usage is not None
    ]
    primary, named = _panel_columns(reports)
    column_count = 3 + len(primary) + 2 * len(named)
    table = _build_table(namew, primary, named, cursor=True)
    for position, row in enumerate(provider.rows):
        if position:
            table.add_row(*([Text("")] * column_count))
        report = (
            row.usage.report
            if isinstance(row, DashboardAccount) and row.usage is not None
            else None
        )
        windows = {} if report is None else _window_index(report)
        usage_row: list[RenderableType] = [
            cursor_account_dot(row, cursor),
            Text(row_label(row), style="grey85"),
            _plan_text(row_plan(row)),
        ]
        reset_row: list[RenderableType] = [
            Text(""),
            Text(""),
            Text(""),
        ]
        for length in primary:
            window = windows.get(("", length))
            usage_row.append(_util_cell(window))
            reset_row.append(_reset_or_blank(window, reference_time))
        for group, lengths in named:
            usage_row.extend(
                (
                    _rule_cell(),
                    _model_subgrid(
                        [
                            _util_cell(windows.get((group, length)))
                            for length in lengths
                        ]
                    ),
                )
            )
            reset_row.extend(
                (
                    _rule_cell(),
                    _model_subgrid(
                        [
                            _reset_or_blank(
                                windows.get((group, length)),
                                reference_time,
                            )
                            for length in lengths
                        ]
                    ),
                )
            )
        table.add_row(*usage_row)
        if report is not None:
            table.add_row(*reset_row)
    blocks: list[RenderableType] = [table]
    for row in provider.rows:
        blocks.extend(
            Text(
                f"⚠ {row_label(row)}: {detail}",
                style="yellow",
            )
            for detail in row_details(row, reference_time)
        )
    color = PROVIDER_COLORS.get(provider.provider_id, "white")
    account_noun = "account" if len(provider.rows) == 1 else "accounts"
    title = Text()
    title.append(provider.provider_id.upper(), style=f"bold {color}")
    title.append(
        f" · {len(provider.rows)} {account_noun}",
        style="grey54",
    )
    subtitle = panel_activity_text(activity) if activity is not None else None
    return Panel(
        Group(*blocks),
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=color,
        padding=(1, 2),
        expand=False,
    )
