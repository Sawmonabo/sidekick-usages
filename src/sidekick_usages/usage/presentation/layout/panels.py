"""Shared provider-panel layout primitives."""

import re
from datetime import datetime

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.models import UsageReport, UsageWindow
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.layout.accounts import plan_text
from sidekick_usages.usage.presentation.layout.models import ProviderPanelRow
from sidekick_usages.usage.presentation.reset import compact_reset_text

HEAT_BANDS: tuple[tuple[int, str, str], ...] = (
    (90, "#ffe6e6", "#b03030"),
    (70, "#fff4e0", "#9c6f12"),
    (40, "#e2fbff", "#1b6a87"),
    (1, "#dfffe9", "#1d5e35"),
)
IDLE_FOREGROUND = "grey39"
ZERO_FOREGROUND = "#cdd3d8"
ZERO_BACKGROUND = "#353a40"
TILE_WIDTH = 6
RULE_STYLE = "#356f78"
PANEL_CHROME = 6
WINDOW_LENGTH_PATTERN = re.compile(r"\d+[hd]")


def _classify_window(name: str) -> tuple[str, str]:
    """Split a window name into its detected length and optional group."""
    match = WINDOW_LENGTH_PATTERN.search(name)
    if match is None:
        return (name.strip(), "")
    length = match.group(0)
    group = (name[: match.start()] + name[match.end() :]).strip()
    return (length, group)


def _length_hours(length: str) -> int:
    """Return an hour-based sort key for one length token."""
    match = WINDOW_LENGTH_PATTERN.fullmatch(length)
    if match is None:
        return 0
    value = int(length[:-1])
    return value * 24 if length[-1] == "d" else value


def _heat_band(percent: int) -> tuple[str, str] | None:
    """Return the utilization colors for one rounded percentage."""
    for threshold, foreground, background in HEAT_BANDS:
        if percent >= threshold:
            return (foreground, background)
    return None


def _heat_tile(percent: int) -> Text:
    """Build one fixed-width utilization tile."""
    band = _heat_band(percent)
    if band is None:
        return Text(
            f"{'0%':^{TILE_WIDTH}}",
            style=f"{ZERO_FOREGROUND} on {ZERO_BACKGROUND}",
        )
    foreground, background = band
    return Text(
        f"{f'{percent}%':^{TILE_WIDTH}}",
        style=f"{foreground} on {background}",
    )


def _reset_cell(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Build one fixed-width reset-countdown cell."""
    return Text(
        f"{compact_reset_text(reset_at, reference_time):^{TILE_WIDTH}}",
        style="grey42",
    )


def panel_columns(
    reports: list[UsageReport],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Derive primary and named-group columns from usage reports."""
    primary_windows: dict[str, int] = {}
    grouped_windows: dict[str, dict[str, int]] = {}
    for report in reports:
        for window in report.windows:
            length, group = _classify_window(window.name)
            hours = _length_hours(length)
            if group == "":
                primary_windows[length] = hours
            else:
                grouped_windows.setdefault(group, {})[length] = hours
    primary = sorted(
        primary_windows,
        key=lambda length: primary_windows[length],
    )
    grouped = [
        (
            group,
            sorted(lengths, key=lambda length: lengths[length]),
        )
        for group, lengths in sorted(grouped_windows.items())
    ]
    return primary, grouped


def window_index(
    report: UsageReport,
) -> dict[tuple[str, str], UsageWindow]:
    """Map each group and length pair to its usage window."""
    index: dict[tuple[str, str], UsageWindow] = {}
    for window in report.windows:
        length, group = _classify_window(window.name)
        index[(group, length)] = window
    return index


def utilization_cell(window: UsageWindow | None) -> Text:
    """Render one utilization tile or an empty cell."""
    return (
        Text("") if window is None else _heat_tile(round(window.utilization))
    )


def reset_or_blank(
    window: UsageWindow | None,
    reference_time: datetime,
) -> Text:
    """Render one reset cell or an empty cell."""
    return (
        Text("")
        if window is None
        else _reset_cell(window.resets_at, reference_time)
    )


def rule_cell() -> Text:
    """Render the divider between primary and named-group columns."""
    return Text("│", style=RULE_STYLE)


def _model_width(name: str, length_count: int) -> int:
    """Return the width required for a named-group column."""
    tiles = TILE_WIDTH * length_count + 2 * (length_count - 1)
    return max(len(name), tiles)


def model_subgrid(cells: list[Text]) -> Table:
    """Lay out one named group's length cells."""
    grid = Table.grid(padding=(0, 1))
    for _cell in cells:
        grid.add_column(width=TILE_WIDTH, justify="center")
    grid.add_row(*cells)
    return grid


def build_table(
    name_width: int,
    primary: list[str],
    grouped: list[tuple[str, list[str]]],
    *,
    cursor: bool = False,
) -> Table:
    """Build the shared provider-table column and header layout."""
    table = Table(
        box=None,
        show_header=False,
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column(width=3 if cursor else 1)
    table.add_column(width=name_width)
    table.add_column(width=4)
    for _length in primary:
        table.add_column(width=TILE_WIDTH, justify="center")
    for group, lengths in grouped:
        table.add_column(width=1, justify="center")
        table.add_column(
            width=_model_width(group, len(lengths)),
            justify="left",
        )
    blank = Text("")
    if grouped:
        caption: list[Text] = [blank, blank, blank]
        caption.extend(blank for _length in primary)
        for group, _lengths in grouped:
            caption.extend((blank, Text(group, style="grey46")))
        table.add_row(*caption)
        column_count = 3 + len(primary) + 2 * len(grouped)
        table.add_row(*([blank] * column_count))
    header: list[RenderableType] = [blank, blank, blank]
    header.extend(Text(length, style="grey42") for length in primary)
    for _group, lengths in grouped:
        header.extend(
            (
                rule_cell(),
                model_subgrid(
                    [Text(length, style="grey42") for length in lengths]
                ),
            )
        )
    table.add_row(*header)
    return table


def provider_usage_table(
    rows: list[ProviderPanelRow],
    name_width: int,
    reference_time: datetime,
    *,
    cursor: bool = False,
) -> Table:
    """Build shared provider usage rows from presentation-only inputs."""
    reports = [row.report for row in rows if row.report is not None]
    primary, grouped = panel_columns(reports)
    column_count = 3 + len(primary) + 2 * len(grouped)
    table = build_table(name_width, primary, grouped, cursor=cursor)
    for position, row in enumerate(rows):
        if position:
            table.add_row(*([Text("")] * column_count))
        windows = {} if row.report is None else window_index(row.report)
        utilization_row: list[RenderableType] = [
            row.marker,
            Text(row.label, style="grey85"),
            plan_text(row.plan),
        ]
        reset_row: list[RenderableType] = [Text(""), Text(""), Text("")]
        for length in primary:
            window = windows.get(("", length))
            utilization_row.append(utilization_cell(window))
            reset_row.append(reset_or_blank(window, reference_time))
        for group, lengths in grouped:
            utilization_row.extend(
                (
                    rule_cell(),
                    model_subgrid(
                        [
                            utilization_cell(windows.get((group, length)))
                            for length in lengths
                        ]
                    ),
                )
            )
            reset_row.extend(
                (
                    rule_cell(),
                    model_subgrid(
                        [
                            reset_or_blank(
                                windows.get((group, length)),
                                reference_time,
                            )
                            for length in lengths
                        ]
                    ),
                )
            )
        table.add_row(*utilization_row)
        if row.report is not None:
            table.add_row(*reset_row)
    return table


def provider_panel_frame(
    provider_id: ProviderId,
    account_count: int,
    content: RenderableType,
    subtitle: str | Text | None,
) -> Panel:
    """Frame one provider's rows, warnings, and activity summary."""
    color = PROVIDER_COLORS.get(provider_id, "white")
    account_noun = "account" if account_count == 1 else "accounts"
    title = Text()
    title.append(provider_id.upper(), style=f"bold {color}")
    title.append(
        f" · {account_count} {account_noun}",
        style="grey54",
    )
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


def panel_min_width(measure: Console, panel: Panel) -> int:
    """Return the natural panel width including border labels."""
    width = measure.measure(panel).maximum
    for label in (panel.title, panel.subtitle):
        if label is not None:
            width = max(
                width,
                measure.measure(label).maximum + PANEL_CHROME,
            )
    return width


def legend() -> Text:
    """Build the utilization heat legend."""
    rendered = Text()
    for label, sample in (
        ("<40", 20),
        ("40-69", 55),
        ("70-89", 80),
        ("≥90", 95),
    ):
        band = _heat_band(sample)
        foreground, background = (
            band if band is not None else (IDLE_FOREGROUND, "default")
        )
        rendered.append(
            f" {label} ",
            style=f"{foreground} on {background}",
        )
        rendered.append("  ")
    rendered.append("   dim = resets in", style="grey42")
    return rendered
