"""Shared narrow-terminal account usage layout."""

from datetime import datetime

from rich.console import Group, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.core.accounts.types import MetricsFreshness
from sidekick_usages.core.models import UsageReport
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.formatting import (
    NARROW_BAR_WIDTH,
    utilization_bar_segments,
)
from sidekick_usages.usage.presentation.layout.accounts import account_header
from sidekick_usages.usage.presentation.reset import reset_text
from sidekick_usages.usage.presentation.theme import (
    ADVISORY_STYLE,
    HEADER_STYLE,
    heat_role,
    usage_style,
)


def _utilization_style(percent: float) -> Style:
    return rich_style(usage_style(heat_role(percent)))


def _braille_bar(percent: float, width: int = NARROW_BAR_WIDTH) -> Text:
    filled, idle = utilization_bar_segments(percent, width)
    bar = Text()
    bar.append(filled, style=_utilization_style(percent))
    bar.append(idle, style="dim")
    return bar


def usage_block(
    label: str,
    provider_id: ProviderId,
    plan: str,
    report: UsageReport,
    observed_at: datetime,
    freshness: MetricsFreshness,
    reference_time: datetime,
    *,
    marker: Text | None = None,
    show_freshness: bool = True,
) -> RenderableType:
    """Render one complete narrow-terminal account usage block."""
    freshness_line = (
        Text(
            "  Last known · " + observed_at.isoformat(),
            style=rich_style(ADVISORY_STYLE),
        )
        if show_freshness and freshness is MetricsFreshness.STALE
        else None
    )
    windows = report.active_windows()
    if not windows:
        lines: list[RenderableType] = [
            account_header(
                label,
                provider_id,
                plan,
                marker=marker,
            ),
        ]
        if freshness_line is not None:
            lines.append(freshness_line)
        lines.append(Text("  No active usage windows reported.", style="dim"))
        return Group(*lines)

    table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column(
        "name",
        style=rich_style(HEADER_STYLE),
        no_wrap=True,
    )
    table.add_column("bar", no_wrap=True)
    table.add_column("pct", justify="right", no_wrap=True)
    table.add_column("reset", no_wrap=True)
    for window in windows:
        percent = round(window.utilization)
        table.add_row(
            f" {window.name}",
            _braille_bar(window.utilization),
            Text(
                f"{percent}%",
                style=_utilization_style(window.utilization),
            ),
            reset_text(window.resets_at, reference_time),
        )
    blocks: list[RenderableType] = [
        account_header(
            label,
            provider_id,
            plan,
            marker=marker,
        ),
    ]
    if freshness_line is not None:
        blocks.append(freshness_line)
    blocks.append(table)
    return Group(*blocks)
