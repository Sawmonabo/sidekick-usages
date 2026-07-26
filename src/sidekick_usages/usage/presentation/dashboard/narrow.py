"""Interactive dashboard narrow-terminal presentation."""

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text

from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    row_details,
    row_label,
    row_marker,
    row_plan,
)
from sidekick_usages.usage.presentation.layout.accounts import account_header
from sidekick_usages.usage.presentation.layout.activity import (
    narrow_activity_summary,
    provider_activity_lines,
)
from sidekick_usages.usage.presentation.layout.narrow import usage_block


def dashboard_narrow_overview(
    snapshot: DashboardSnapshot,
    cursor: DashboardCursor,
    activities: dict[ProviderId, TokenActivitySummary],
) -> RenderableType:
    """Render interactive rows and provider totals for narrow terminals."""
    blocks: list[RenderableType] = []
    for provider in snapshot.providers:
        if not provider.rows:
            continue
        for row in provider.rows:
            if blocks:
                blocks.append(Text(""))
            marker = row_marker(row, cursor)
            if isinstance(row, DashboardAccount) and row.usage is not None:
                blocks.append(
                    usage_block(
                        row.label,
                        row.provider_id,
                        row.plan,
                        row.usage.report,
                        row.usage.observed_at,
                        row.usage.freshness,
                        snapshot.reference_time,
                        marker=marker,
                        show_freshness=False,
                    )
                )
            else:
                blocks.append(
                    account_header(
                        row_label(row),
                        row.provider_id,
                        row_plan(row),
                        marker=marker,
                    )
                )
            blocks.extend(
                Padding(
                    Text(f"⚠ {detail}", style="yellow"),
                    (0, 0, 0, 4),
                )
                for detail in row_details(row, snapshot.reference_time)
            )
        activity = activities.get(provider.provider_id)
        if activity is None:
            continue
        blocks.append(Text(""))
        blocks.extend(
            provider_activity_lines(
                provider.provider_id,
                narrow_activity_summary(activity),
            )
        )
    return Group(*blocks)
