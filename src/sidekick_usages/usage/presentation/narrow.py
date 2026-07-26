"""Narrow-terminal per-account usage presentation."""

from datetime import datetime

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardSnapshot,
)
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    MetricsFreshness,
    ProviderTokenActivity,
    TokenActivityIssue,
    UsageCheckResult,
)
from sidekick_usages.usage.presentation.activity import (
    activity_failure_label,
    narrow_activity_lines,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    PLAN_COLORS,
    account_activity_issues,
    activity_issue_copy,
    failure_copy,
    row_details,
    row_label,
    row_marker,
    row_plan,
)
from sidekick_usages.usage.presentation.reset import reset_text

BAR_WIDTH = 18

_PCT_RED_THRESHOLD = 90
_PCT_YELLOW_THRESHOLD = 70
_PCT_CYAN_THRESHOLD = 40


def _utilization_color(percent: float) -> str:
    if percent >= _PCT_RED_THRESHOLD:
        return "red"
    if percent >= _PCT_YELLOW_THRESHOLD:
        return "yellow"
    if percent >= _PCT_CYAN_THRESHOLD:
        return "cyan"
    return "green"


def _braille_bar(percent: float, width: int = BAR_WIDTH) -> Text:
    bounded = max(0.0, min(100.0, percent))
    filled = round(bounded / 100.0 * width)
    bar = Text()
    bar.append("⣿" * filled, style=_utilization_color(bounded))
    bar.append("⣀" * (width - filled), style="dim")
    return bar


def _account_tag(provider_id: ProviderId, plan: str) -> Text:
    provider_color = PROVIDER_COLORS.get(provider_id, "dim")
    tag = Text("[", style="dim")
    tag.append(provider_id, style=provider_color)
    if plan and plan != "unknown":
        tag.append(" · ", style="dim")
        tag.append(plan, style=PLAN_COLORS.get(plan, "dim"))
    tag.append("]", style="dim")
    return tag


def account_header(
    label: str,
    provider_id: ProviderId,
    plan: str,
    *,
    marker: Text | None = None,
) -> Text:
    """Render a standalone account label and provider-plan tag."""
    header = Text()
    if marker is not None:
        header.append_text(marker)
    header.append(label, style="bold")
    header.append("  ")
    header.append_text(_account_tag(provider_id, plan))
    return header


def usage_report(
    usage: AccountUsage,
    reference_time: datetime,
    *,
    marker: Text | None = None,
    show_freshness: bool = True,
) -> RenderableType:
    """Render one complete narrow-terminal account usage block."""
    freshness = (
        Text(
            "  Last known · " + usage.fetched_at.isoformat(),
            style="yellow",
        )
        if show_freshness and usage.freshness is MetricsFreshness.STALE
        else None
    )
    windows = usage.report.active_windows()
    if not windows:
        lines: list[RenderableType] = [
            account_header(
                usage.label,
                usage.provider_id,
                usage.plan,
                marker=marker,
            ),
        ]
        if freshness is not None:
            lines.append(freshness)
        lines.append(Text("  No active usage windows reported.", style="dim"))
        return Group(
            *lines,
        )

    table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column("name", style="dim", no_wrap=True)
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
                style=_utilization_color(window.utilization),
            ),
            reset_text(window.resets_at, reference_time),
        )
    blocks: list[RenderableType] = [
        account_header(
            usage.label,
            usage.provider_id,
            usage.plan,
            marker=marker,
        ),
    ]
    if freshness is not None:
        blocks.append(freshness)
    blocks.append(table)
    return Group(*blocks)


def _failure_block(failure: FetchFailure) -> Group:
    """Stack one failure in the supported narrow view."""
    status, detail = failure_copy(failure)
    lines: list[RenderableType] = [
        account_header(
            failure.label,
            failure.provider_id,
            failure.plan,
        ),
        Text(f"  ⚠ {status}", style="yellow"),
    ]
    lines.extend(Text(f"  {line}", style="grey54") for line in detail)
    return Group(*lines)


def _activity_issue_block(
    provider_id: ProviderId,
    issue: TokenActivityIssue,
    plan: str,
) -> Group:
    """Stack one activity warning in the narrow fallback."""
    if issue.label is None:
        raise ValueError("Account activity issue requires a label.")
    lines: list[RenderableType] = [
        account_header(issue.label, provider_id, plan),
        Text(f"  ⚠ {activity_failure_label(issue.kind)}", style="yellow"),
    ]
    lines.extend(
        Text(f"  {line}", style="grey54")
        for line in activity_issue_copy(provider_id, issue)
    )
    return Group(*lines)


def _narrow_activity_blocks(
    result: UsageCheckResult,
    provider_ids: tuple[ProviderId, ...],
) -> list[RenderableType]:
    """Build compact provider activity summaries and warning blocks."""
    blocks: list[RenderableType] = []
    activities = {
        activity.provider_id: activity for activity in result.activities
    }
    plans = {
        (item.provider_id, item.label): item.plan
        for item in (*result.usages, *result.failures)
    }
    for provider_id in provider_ids:
        activity = activities.get(provider_id)
        if activity is None:
            continue
        if blocks:
            blocks.append(Text(""))
        provider_name = provider_id.upper()
        color = PROVIDER_COLORS.get(provider_id, "white")
        prefix_width = len(provider_name) + len(" · ")
        for position, activity_line in enumerate(
            narrow_activity_lines(activity)
        ):
            line = Text()
            if position == 0:
                line.append(provider_name, style=f"bold {color}")
                line.append(" · ", style="grey54")
            else:
                line.append(" " * prefix_width)
            line.append_text(activity_line)
            blocks.append(line)
        for issue in account_activity_issues(activity):
            if blocks:
                blocks.append(Text(""))
            if issue.label is None:
                raise ValueError("Account activity issue requires a label.")
            blocks.append(
                _activity_issue_block(
                    provider_id,
                    issue,
                    plans.get((provider_id, issue.label), "unknown"),
                )
            )
    return blocks


def narrow_overview(
    result: UsageCheckResult,
    provider_ids: tuple[ProviderId, ...],
) -> RenderableType:
    """Stack one-shot account results for narrow terminals."""
    blocks: list[RenderableType] = []
    for index, usage in enumerate(result.usages):
        if index:
            blocks.append(Text(""))
        blocks.append(usage_report(usage, result.reference_time))
    for failure in result.failures:
        if blocks:
            blocks.append(Text(""))
        blocks.append(_failure_block(failure))
    activity_blocks = _narrow_activity_blocks(result, provider_ids)
    if blocks and activity_blocks:
        blocks.append(Text(""))
    blocks.extend(activity_blocks)
    return Group(*blocks)


def dashboard_narrow_overview(
    snapshot: DashboardSnapshot,
    cursor: DashboardCursor,
    activities: dict[ProviderId, ProviderTokenActivity],
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
                    usage_report(
                        AccountUsage(
                            account_id=row.account_id,
                            label=row.label,
                            provider_id=row.provider_id,
                            plan=row.plan,
                            report=row.usage.report,
                            fetched_at=row.usage.observed_at,
                            freshness=MetricsFreshness.STALE,
                        ),
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
        provider_name = provider.provider_id.upper()
        provider_color = PROVIDER_COLORS.get(provider.provider_id, "white")
        prefix_width = len(provider_name) + len(" · ")
        for position, activity_line in enumerate(
            narrow_activity_lines(activity)
        ):
            line = Text()
            if position == 0:
                line.append(provider_name, style=f"bold {provider_color}")
                line.append(" · ", style="grey54")
            else:
                line.append(" " * prefix_width)
            line.append_text(activity_line)
            blocks.append(line)
    return Group(*blocks)
