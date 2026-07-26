"""One-shot narrow-terminal usage presentation."""

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.models import (
    FetchFailure,
    ProviderTokenActivity,
    TokenActivityIssue,
    UsageCheckResult,
)
from sidekick_usages.usage.presentation.activity import (
    activity_failure_label,
    narrow_activity_lines,
)
from sidekick_usages.usage.presentation.failures import (
    account_activity_issues,
    activity_issue_copy,
    failure_copy,
)
from sidekick_usages.usage.presentation.layout.accounts import account_header
from sidekick_usages.usage.presentation.layout.activity import (
    provider_activity_lines,
)
from sidekick_usages.usage.presentation.layout.narrow import usage_block
from sidekick_usages.usage.presentation.theme import ADVISORY_STYLE


def _failure_block(failure: FetchFailure) -> Group:
    """Stack one failure in the supported narrow view."""
    status, detail = failure_copy(failure)
    lines: list[RenderableType] = [
        account_header(
            failure.label,
            failure.provider_id,
            failure.plan,
        ),
        Text(
            f"  ⚠ {status}",
            style=rich_style(ADVISORY_STYLE),
        ),
    ]
    lines.extend(
        Text(f"  {line}", style=rich_style(ADVISORY_STYLE)) for line in detail
    )
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
        Text(
            f"  ⚠ {activity_failure_label(issue.kind)}",
            style=rich_style(ADVISORY_STYLE),
        ),
    ]
    lines.extend(
        Text(f"  {line}", style=rich_style(ADVISORY_STYLE))
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
        activity: ProviderTokenActivity | None = activities.get(provider_id)
        if activity is None:
            continue
        if blocks:
            blocks.append(Text(""))
        blocks.extend(
            provider_activity_lines(
                provider_id,
                narrow_activity_lines(activity),
            )
        )
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
        blocks.append(
            usage_block(
                usage.label,
                usage.provider_id,
                usage.plan,
                usage.report,
                usage.fetched_at,
                usage.freshness,
                result.reference_time,
            )
        )
    for failure in result.failures:
        if blocks:
            blocks.append(Text(""))
        blocks.append(_failure_block(failure))
    activity_blocks = _narrow_activity_blocks(result, provider_ids)
    if blocks and activity_blocks:
        blocks.append(Text(""))
    blocks.extend(activity_blocks)
    return Group(*blocks)
