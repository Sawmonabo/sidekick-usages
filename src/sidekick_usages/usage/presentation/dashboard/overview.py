"""Interactive dashboard overview orchestration."""

from rich.console import Console, Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import FULL_HEADER_MIN_WIDTH, brand_header
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardFooter,
    DashboardProvider,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.footer import (
    footer_renderable,
)
from sidekick_usages.usage.presentation.dashboard.narrow import (
    dashboard_narrow_overview,
)
from sidekick_usages.usage.presentation.dashboard.panels import (
    dashboard_provider_panel,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    row_is_selected,
    row_label,
)
from sidekick_usages.usage.presentation.layout.panels import (
    legend,
    panel_min_width,
)


def _dashboard_activity(
    provider: DashboardProvider,
) -> TokenActivitySummary | None:
    observations = tuple(
        row.activity
        for row in provider.rows
        if isinstance(row, DashboardAccount) and row.activity is not None
    )
    if not observations:
        return None
    scopes = {observation.summary.scope for observation in observations}
    if len(scopes) != 1:
        raise ValueError("Dashboard provider activity scopes must agree.")
    scope = next(iter(scopes))
    if scope is TokenActivityScope.LOCAL_INSTALLATION:
        return max(
            observations,
            key=lambda observation: observation.observed_at,
        ).summary
    since_values = tuple(
        observation.summary.since for observation in observations
    )
    return TokenActivitySummary(
        total_tokens=sum(
            observation.summary.total_tokens for observation in observations
        ),
        scope=scope,
        since=(
            min(since_values)
            if all(value is not None for value in since_values)
            else None
        ),
    )


def dashboard_overview(
    snapshot: DashboardSnapshot,
    *,
    width: int,
    cursor: DashboardCursor,
    footer: DashboardFooter,
) -> RenderableType:
    """Render one interactive dashboard with exactly one visible cursor."""
    providers = tuple(
        provider for provider in snapshot.providers if provider.rows
    )
    rows = tuple(row for provider in providers for row in provider.rows)
    if not rows:
        return Group(
            brand_header(width),
            Text(""),
            Text("No accounts to display.", style="dim"),
            Text(""),
            footer_renderable(footer),
        )
    if sum(row_is_selected(row, cursor) for row in rows) != 1:
        raise ValueError("Interactive dashboard requires exactly one cursor.")
    namew = max(len(row_label(row)) for row in rows)
    activities = {
        provider.provider_id: activity
        for provider in providers
        if (activity := _dashboard_activity(provider)) is not None
    }
    measure = Console(width=10_000)
    panels = [
        dashboard_provider_panel(
            provider,
            cursor,
            namew,
            activities.get(provider.provider_id),
            snapshot.reference_time,
        )
        for provider in providers
    ]
    required = max(
        FULL_HEADER_MIN_WIDTH,
        *(panel_min_width(measure, panel) for panel in panels),
    )
    if width < required:
        return Group(
            brand_header(width),
            Text(""),
            dashboard_narrow_overview(snapshot, cursor, activities),
            Text(""),
            footer_renderable(footer),
        )
    for panel in panels:
        panel.expand = True
        panel.width = required
    parts: list[RenderableType] = [
        Text(""),
        brand_header(required),
        Text(""),
    ]
    for panel in panels:
        parts.extend((panel, Text("")))
    parts.extend((legend(), Text(""), footer_renderable(footer), Text("")))
    return Group(*parts)
