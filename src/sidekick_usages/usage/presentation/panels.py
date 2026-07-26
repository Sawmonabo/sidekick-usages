"""One-shot provider-panel presentation."""

from collections.abc import Sequence
from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.core.accounts.types import MetricsFreshness
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    ProviderTokenActivity,
    TokenActivityIssue,
)
from sidekick_usages.usage.presentation.activity import (
    activity_failure_label,
    panel_activity_text,
)
from sidekick_usages.usage.presentation.failures import (
    account_activity_issues,
    activity_issue_copy,
    failure_copy,
)
from sidekick_usages.usage.presentation.layout.accounts import (
    account_dot,
    plan_text,
)
from sidekick_usages.usage.presentation.layout.models import ProviderPanelRow
from sidekick_usages.usage.presentation.layout.panels import (
    provider_panel_frame,
    provider_usage_table,
)
from sidekick_usages.usage.presentation.theme import (
    ACCOUNT_LABEL_STYLE,
    ADVISORY_STYLE,
)


def provider_panel(
    provider_id: ProviderId,
    usages: list[AccountUsage],
    failures: list[FetchFailure],
    name_width: int,
    activity: ProviderTokenActivity | None,
    reference_time: datetime,
) -> Panel:
    """Build one provider panel for a one-shot usage result."""
    blocks: list[RenderableType] = []
    if usages:
        blocks.append(
            provider_usage_table(
                [
                    ProviderPanelRow(
                        marker=account_dot(provider_id),
                        label=usage.label,
                        plan=usage.plan,
                        report=usage.report,
                    )
                    for usage in usages
                ],
                name_width,
                reference_time,
            )
        )
        blocks.extend(_stale_usage_lines(usages))
    if failures:
        if blocks:
            blocks.append(Text(""))
        blocks.append(_error_table(provider_id, failures, name_width))
    activity_issues = account_activity_issues(activity)
    if activity_issues:
        if blocks:
            blocks.append(Text(""))
        blocks.append(
            _activity_issue_table(
                provider_id,
                activity_issues,
                name_width,
            )
        )
    content: RenderableType = blocks[0] if len(blocks) == 1 else Group(*blocks)
    account_count = len(
        {usage.label for usage in usages}
        | {failure.label for failure in failures}
    )
    subtitle = panel_activity_text(activity) if activity is not None else None
    return provider_panel_frame(
        provider_id,
        account_count,
        content,
        subtitle,
    )


def _stale_usage_lines(usages: Sequence[AccountUsage]) -> tuple[Text, ...]:
    """Return visible timestamped warnings for retained usage rows."""
    return tuple(
        Text(
            f"⚠ {usage.label}: last known · {usage.fetched_at.isoformat()}",
            style=rich_style(ADVISORY_STYLE),
        )
        for usage in usages
        if usage.freshness is MetricsFreshness.STALE
    )


def _error_table(
    provider_id: ProviderId,
    failures: list[FetchFailure],
    name_width: int,
) -> Table:
    """Build account-aligned provider failure rows."""
    rows: list[tuple[str, str, Text, tuple[Text, ...]]] = []
    for failure in failures:
        status_label, detail_lines = failure_copy(failure)
        status = Text(
            f"⚠ {status_label}",
            style=rich_style(ADVISORY_STYLE),
        )
        detail = tuple(
            Text(line, style=rich_style(ADVISORY_STYLE))
            for line in detail_lines
        )
        rows.append((failure.label, failure.plan, status, detail))
    return _warning_table(provider_id, rows, name_width)


def _warning_table(
    provider_id: ProviderId,
    rows: list[tuple[str, str, Text, tuple[Text, ...]]],
    name_width: int,
) -> Table:
    """Align account warnings with the shared account columns."""
    rest_width = max(
        1,
        *(
            text.cell_len
            for _label, _plan, status, detail in rows
            for text in (status, *detail)
        ),
    )
    table = Table(
        box=None,
        show_header=False,
        padding=(0, 1),
        pad_edge=False,
    )
    for width in (1, name_width, 4, rest_width):
        table.add_column(width=width)
    for position, (label, plan, status, detail) in enumerate(rows):
        if position:
            table.add_row(*([Text("")] * 4))
        table.add_row(
            account_dot(provider_id),
            Text(label, style=rich_style(ACCOUNT_LABEL_STYLE)),
            plan_text(plan),
            Group(status, *detail),
        )
    return table


def _activity_issue_table(
    provider_id: ProviderId,
    issues: tuple[TokenActivityIssue, ...],
    name_width: int,
) -> Table:
    """Build account-aligned warning rows for activity read failures."""
    rows: list[tuple[str, str, Text, tuple[Text, ...]]] = []
    for issue in issues:
        if issue.label is None:
            raise ValueError("Account activity issue requires a label.")
        status = Text(
            f"⚠ {activity_failure_label(issue.kind)}",
            style=rich_style(ADVISORY_STYLE),
        )
        detail = tuple(
            Text(line, style=rich_style(ADVISORY_STYLE))
            for line in activity_issue_copy(provider_id, issue)
        )
        rows.append((issue.label, "unknown", status, detail))
    return _warning_table(provider_id, rows, name_width)


def provider_order(
    usages: Sequence[AccountUsage],
    failures: Sequence[FetchFailure] = (),
) -> list[ProviderId]:
    """Return provider IDs in their first observed result order."""
    order: list[ProviderId] = []
    provider_ids = (
        *(usage.provider_id for usage in usages),
        *(failure.provider_id for failure in failures),
    )
    for provider_id in provider_ids:
        if provider_id not in order:
            order.append(provider_id)
    return order
