"""Canonical dashboard-frame orchestration."""

from sidekick_usages.branding.content import FULL_HEADER_MIN_WIDTH
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardFooter,
    DashboardProvider,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
)
from sidekick_usages.usage.presentation.dashboard.render.narrow import (
    render_narrow,
)
from sidekick_usages.usage.presentation.dashboard.render.style import (
    render_dashboard_lines,
)
from sidekick_usages.usage.presentation.dashboard.render.text import (
    brand_lines,
    clip_line,
    footer_lines,
    line_width,
    plain_line,
)
from sidekick_usages.usage.presentation.dashboard.render.wide import (
    dashboard_required_width,
    render_wide,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    row_is_selected,
    row_label,
)
from sidekick_usages.usage.presentation.formatting import (
    cell_width,
    sanitize_terminal_text,
)
from sidekick_usages.usage.presentation.theme import UsageTextRole


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    width: int,
    cursor: DashboardCursor,
    footer: DashboardFooter,
    color: bool,
) -> str:
    """Render one complete cached or interactive dashboard frame."""
    safe_width = max(1, width)
    providers = tuple(
        provider for provider in snapshot.providers if provider.rows
    )
    rows = tuple(row for provider in providers for row in provider.rows)
    rendered_footer = footer_lines(footer, safe_width)
    if not rows:
        lines = [
            *brand_lines(safe_width),
            DashboardLine(),
            plain_line(
                "No accounts to display.",
                UsageTextRole.DIM,
            ),
            DashboardLine(),
            *rendered_footer,
        ]
        return _finish(lines, safe_width, color)
    selected_rows = sum(row_is_selected(row, cursor) for row in rows)
    provider_only_focus = (
        cursor.focused_provider
        in {provider.provider_id for provider in providers}
        and cursor.account_id is None
    )
    if selected_rows != 1 and not (selected_rows == 0 and provider_only_focus):
        raise ValueError(
            "Interactive dashboard requires one row or provider focus."
        )

    activities = {
        provider.provider_id: activity
        for provider in providers
        if (activity := _dashboard_activity(provider)) is not None
    }
    name_width = max(
        cell_width(sanitize_terminal_text(row_label(row))) for row in rows
    )
    required_width = max(
        FULL_HEADER_MIN_WIDTH,
        dashboard_required_width(
            providers,
            name_width,
            activities,
        ),
    )
    if safe_width < required_width:
        lines = render_narrow(
            snapshot,
            cursor,
            activities,
            safe_width,
            rendered_footer,
        )
    else:
        lines = render_wide(
            snapshot,
            cursor,
            activities,
            name_width,
            required_width,
            rendered_footer,
        )
    return _finish(lines, safe_width, color)


def _finish(
    lines: list[DashboardLine],
    width: int,
    color: bool,
) -> str:
    bounded = [clip_line(line, width) for line in lines]
    if any(line_width(line) > width for line in bounded):
        raise RuntimeError("Dashboard line exceeded terminal width.")
    return render_dashboard_lines(bounded, color=color)


def _dashboard_activity(
    provider: DashboardProvider,
) -> TokenActivitySummary | None:
    if provider.activity is not None:
        return provider.activity.summary
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
    if scope is not TokenActivityScope.ACCOUNT:
        raise ValueError("Dashboard account activity scope is invalid.")
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
