"""Responsive narrow-dashboard semantic account blocks."""

from datetime import datetime

from sidekick_usages.core.models import TokenActivitySummary, UsageReport
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardExternalRow,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
    DashboardTextStyle,
)
from sidekick_usages.usage.presentation.dashboard.render.text import (
    brand_lines,
    concat_lines,
    fit_line,
    heat_style,
    line,
    plain_line,
    plan_style,
    provider_style,
    segment,
    visible_plan,
    wrap_text,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    CURSOR_GLYPH,
    row_details,
    row_is_selected,
    row_label,
    row_plan,
)
from sidekick_usages.usage.presentation.formatting import (
    NARROW_BAR_WIDTH,
    format_reset_text,
    format_since,
    format_tokens_compact,
    sanitize_terminal_text,
    utilization_bar_segments,
)


def render_narrow(
    snapshot: DashboardSnapshot,
    cursor: DashboardCursor,
    activities: dict[ProviderId, TokenActivitySummary],
    width: int,
    rendered_footer: list[DashboardLine],
) -> list[DashboardLine]:
    """Render stacked account blocks within one terminal width."""
    lines = [*brand_lines(width), DashboardLine()]
    has_block = False
    for provider in snapshot.providers:
        if not provider.rows:
            continue
        for row in provider.rows:
            if has_block:
                lines.append(DashboardLine())
            lines.append(_account_header_line(row, cursor))
            if isinstance(row, DashboardAccount) and row.usage is not None:
                lines.extend(
                    _usage_lines(
                        row.usage.report,
                        snapshot.reference_time,
                    )
                )
            for detail in row_details(row, snapshot.reference_time):
                lines.extend(_warning_lines(detail, width))
            has_block = True
        activity = activities.get(provider.provider_id)
        if activity is not None:
            lines.append(DashboardLine())
            lines.extend(_activity_lines(provider.provider_id, activity))
    lines.extend((DashboardLine(), *rendered_footer))
    return lines


def _account_header_line(
    row: DashboardAccount | DashboardExternalRow,
    cursor: DashboardCursor,
) -> DashboardLine:
    selected = row_is_selected(row, cursor)
    safe_plan = visible_plan(row_plan(row))
    parts = [
        segment(
            CURSOR_GLYPH if selected else " ",
            (
                DashboardTextStyle.CURSOR
                if selected
                else DashboardTextStyle.PLAIN
            ),
        ),
        segment(" "),
        segment("●", provider_style(row.provider_id)),
        segment(" "),
        segment(
            sanitize_terminal_text(row_label(row)),
            DashboardTextStyle.LABEL,
        ),
        segment("  [", DashboardTextStyle.DIM),
        segment(str(row.provider_id), provider_style(row.provider_id)),
    ]
    if safe_plan:
        parts.extend(
            (
                segment(" · ", DashboardTextStyle.DIM),
                segment(safe_plan, plan_style(safe_plan)),
            )
        )
    parts.append(segment("]", DashboardTextStyle.DIM))
    return line(*parts)


def _usage_lines(
    report: UsageReport,
    reference_time: datetime,
) -> list[DashboardLine]:
    windows = report.active_windows()
    if not windows:
        return [
            plain_line(
                "  No active usage windows reported.",
                DashboardTextStyle.DIM,
            )
        ]
    lines: list[DashboardLine] = []
    for window in windows:
        percent = round(window.utilization)
        filled, idle = utilization_bar_segments(
            window.utilization,
            NARROW_BAR_WIDTH,
        )
        lines.append(
            line(
                segment(
                    f" {sanitize_terminal_text(window.name)}  ",
                    DashboardTextStyle.HEADER,
                ),
                segment(filled, heat_style(percent)),
                segment(idle, DashboardTextStyle.DIM),
                segment(f"  {percent:>2}%", heat_style(percent)),
                segment("  "),
                segment(
                    format_reset_text(
                        window.resets_at,
                        reference_time,
                    ),
                    DashboardTextStyle.RESET,
                ),
            )
        )
    return lines


def _warning_lines(detail: str, width: int) -> list[DashboardLine]:
    return [
        fit_line(line, width, "left")
        for line in wrap_text(
            sanitize_terminal_text(f"⚠ {detail}"),
            width,
            DashboardTextStyle.WARNING,
            initial_prefix="    ",
            subsequent_prefix="    ",
        )
    ]


def _activity_lines(
    provider_id: ProviderId,
    activity: TokenActivitySummary,
) -> list[DashboardLine]:
    provider = provider_id.upper()
    prefix = f"{provider} · "
    lines = [
        concat_lines(
            plain_line(provider, provider_style(provider_id)),
            plain_line(" · ", DashboardTextStyle.DIM),
            plain_line(
                f"{format_tokens_compact(activity.total_tokens)} tokens",
                DashboardTextStyle.DIM,
            ),
        )
    ]
    if activity.since is not None:
        lines.append(
            line(
                segment(" " * len(prefix)),
                segment(
                    f"since {format_since(activity.since)}",
                    DashboardTextStyle.RESET,
                ),
            )
        )
    return lines
