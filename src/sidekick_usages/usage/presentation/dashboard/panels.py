"""Interactive dashboard provider-panel presentation."""

from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardProvider,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    cursor_account_dot,
    row_details,
    row_label,
    row_plan,
)
from sidekick_usages.usage.presentation.layout.activity import (
    panel_activity_summary,
)
from sidekick_usages.usage.presentation.layout.models import ProviderPanelRow
from sidekick_usages.usage.presentation.layout.panels import (
    provider_panel_frame,
    provider_usage_table,
)


def dashboard_provider_panel(
    provider: DashboardProvider,
    cursor: DashboardCursor,
    name_width: int,
    activity: TokenActivitySummary | None,
    reference_time: datetime,
) -> Panel:
    """Build one provider panel for the interactive dashboard."""
    table = provider_usage_table(
        [
            ProviderPanelRow(
                marker=cursor_account_dot(row, cursor),
                label=row_label(row),
                plan=row_plan(row),
                report=(
                    row.usage.report
                    if isinstance(row, DashboardAccount)
                    and row.usage is not None
                    else None
                ),
            )
            for row in provider.rows
        ],
        name_width,
        reference_time,
        cursor=True,
    )
    blocks: list[RenderableType] = [table]
    for row in provider.rows:
        blocks.extend(
            Text(
                f"⚠ {row_label(row)}: {detail}",
                style="yellow",
            )
            for detail in row_details(row, reference_time)
        )
    subtitle = None if activity is None else panel_activity_summary(activity)
    return provider_panel_frame(
        provider.provider_id,
        len(provider.rows),
        Group(*blocks),
        subtitle,
    )
