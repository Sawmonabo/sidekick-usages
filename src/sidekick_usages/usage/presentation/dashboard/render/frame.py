"""Canonical semantic dashboard-layout orchestration."""

from dataclasses import replace

from sidekick_usages.branding.content import FULL_HEADER_MIN_WIDTH
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardFooter,
    DashboardProvider,
    DashboardSnapshot,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
    DashboardRenderLayout,
    TerminalDimensions,
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
    key_lines,
    line_width,
    plain_line,
    status_lines,
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

MINIMUM_SUPPORTED_ROWS = 24
MINIMUM_USEFUL_BODY_ROWS = 16
ONE_SHOT_ROWS = 60
TERMINAL_TOO_SHORT = DashboardStatus(
    kind=DashboardStatusKind.ERROR,
    message="Terminal too short; scroll to view saved accounts.",
)


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    width: int,
    cursor: DashboardCursor,
    footer: DashboardFooter,
    color: bool,
) -> str:
    """Join semantic fragments into one finite noninteractive frame."""
    layout = render_dashboard_layout(
        snapshot,
        dimensions=TerminalDimensions(
            columns=max(1, width),
            rows=ONE_SHOT_ROWS,
        ),
        cursor=cursor,
        footer=footer,
        color=color,
    )
    parts = [
        layout.masthead.rstrip("\n"),
        "",
        layout.body.rstrip("\n"),
        "",
    ]
    if layout.status:
        parts.extend((layout.status.rstrip("\n"), ""))
    parts.append(layout.keys.rstrip("\n"))
    return "\n".join(parts) + "\n"


def render_dashboard_layout(
    snapshot: DashboardSnapshot,
    *,
    dimensions: TerminalDimensions,
    cursor: DashboardCursor,
    footer: DashboardFooter,
    color: bool,
) -> DashboardRenderLayout:
    """Render independent fragments for one exact terminal viewport."""
    width = dimensions.columns
    body_lines = _body_lines(snapshot, cursor, width)
    resolved_footer = (
        replace(footer, status=TERMINAL_TOO_SHORT)
        if dimensions.rows < MINIMUM_SUPPORTED_ROWS
        else footer
    )
    rendered_status = status_lines(resolved_footer, width)
    rendered_keys = key_lines(resolved_footer, width)
    full_masthead = brand_lines(width)
    fixed_rows = max(1, len(rendered_status)) + len(rendered_keys)
    compact = (
        dimensions.rows - len(full_masthead) - fixed_rows
        < MINIMUM_USEFUL_BODY_ROWS
    )
    rendered_masthead = brand_lines(width, compact=compact)
    focused_line = next(
        (
            position
            for position, rendered in enumerate(body_lines)
            if any(
                segment.style is UsageTextRole.CURSOR
                for segment in rendered.segments
            )
        ),
        None,
    )
    return DashboardRenderLayout(
        masthead=_finish(rendered_masthead, width, color),
        body=_finish(body_lines, width, color),
        status=(
            "" if not rendered_status else _finish(
                rendered_status,
                width,
                color,
            )
        ),
        keys=_finish(rendered_keys, width, color),
        focused_body_line=focused_line,
    )


def _body_lines(
    snapshot: DashboardSnapshot,
    cursor: DashboardCursor,
    width: int,
) -> list[DashboardLine]:
    providers = tuple(
        provider for provider in snapshot.providers if provider.rows
    )
    rows = tuple(row for provider in providers for row in provider.rows)
    if not rows:
        return [
            plain_line(
                "No accounts to display.",
                UsageTextRole.DIM,
            ),
        ]
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
    if width < required_width:
        return render_narrow(
            snapshot,
            cursor,
            activities,
            width,
        )
    return render_wide(
        snapshot,
        cursor,
        activities,
        name_width,
        required_width,
    )


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
