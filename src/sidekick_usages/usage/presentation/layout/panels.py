"""Shared provider-panel layout primitives."""

from datetime import datetime

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding.rich import PROVIDER_COLORS
from sidekick_usages.core.models import UsageWindow
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.formatting import (
    ACTIVE_PERCENT_THRESHOLD,
    CYAN_PERCENT_THRESHOLD,
    PANEL_CHROME_WIDTH,
    PANEL_TILE_WIDTH,
    RED_PERCENT_THRESHOLD,
    YELLOW_PERCENT_THRESHOLD,
    compact_reset_text,
    panel_columns,
    panel_model_width,
    window_index,
)
from sidekick_usages.usage.presentation.layout.accounts import plan_text
from sidekick_usages.usage.presentation.layout.models import ProviderPanelRow

HEAT_BANDS: tuple[tuple[int, str, str], ...] = (
    (RED_PERCENT_THRESHOLD, "#ffe6e6", "#b03030"),
    (YELLOW_PERCENT_THRESHOLD, "#fff4e0", "#9c6f12"),
    (CYAN_PERCENT_THRESHOLD, "#e2fbff", "#1b6a87"),
    (ACTIVE_PERCENT_THRESHOLD, "#dfffe9", "#1d5e35"),
)
IDLE_FOREGROUND = "grey39"
ZERO_FOREGROUND = "#cdd3d8"
ZERO_BACKGROUND = "#353a40"
RULE_STYLE = "#356f78"


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
            f"{'0%':^{PANEL_TILE_WIDTH}}",
            style=f"{ZERO_FOREGROUND} on {ZERO_BACKGROUND}",
        )
    foreground, background = band
    return Text(
        f"{f'{percent}%':^{PANEL_TILE_WIDTH}}",
        style=f"{foreground} on {background}",
    )


def _reset_cell(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Build one fixed-width reset-countdown cell."""
    reset = compact_reset_text(reset_at, reference_time)
    return Text(
        f"{reset:^{PANEL_TILE_WIDTH}}",
        style="grey42",
    )


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


def model_subgrid(cells: list[Text]) -> Table:
    """Lay out one named group's length cells."""
    grid = Table.grid(padding=(0, 1))
    for _cell in cells:
        grid.add_column(width=PANEL_TILE_WIDTH, justify="center")
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
        table.add_column(width=PANEL_TILE_WIDTH, justify="center")
    for group, lengths in grouped:
        table.add_column(width=1, justify="center")
        table.add_column(
            width=panel_model_width(group, len(lengths)),
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
                measure.measure(label).maximum + PANEL_CHROME_WIDTH,
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
