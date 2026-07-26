"""One-shot usage overview orchestration."""

from rich.console import Console, Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import FULL_HEADER_MIN_WIDTH, brand_header
from sidekick_usages.usage.models import UsageCheckResult
from sidekick_usages.usage.presentation.narrow import narrow_overview
from sidekick_usages.usage.presentation.panels import (
    legend,
    panel_min_width,
    provider_order,
    provider_panel,
)


def render_usage_overview(
    result: UsageCheckResult,
    *,
    width: int,
) -> RenderableType:
    """Render the one-shot usage dashboard."""
    if not result.usages and not result.failures:
        return Text("No usage to display.", style="dim")
    labels = [usage.label for usage in result.usages]
    labels.extend(failure.label for failure in result.failures)
    namew = max(len(label) for label in labels)
    order = provider_order(result.usages, result.failures)
    activities = {
        activity.provider_id: activity for activity in result.activities
    }
    measure = Console(width=10_000)
    panels = [
        provider_panel(
            provider_id,
            [
                usage
                for usage in result.usages
                if usage.provider_id == provider_id
            ],
            [
                failure
                for failure in result.failures
                if failure.provider_id == provider_id
            ],
            namew,
            activities.get(provider_id),
            result.reference_time,
        )
        for provider_id in order
    ]
    required = max(
        FULL_HEADER_MIN_WIDTH,
        *(panel_min_width(measure, panel) for panel in panels),
    )
    if width < required:
        return Group(
            brand_header(width),
            Text(""),
            narrow_overview(result, tuple(order)),
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
    parts.extend((legend(), Text("")))
    return Group(*parts)


def usage_overview(
    result: UsageCheckResult,
    *,
    width: int,
) -> RenderableType:
    """Render the stable one-shot usage dashboard."""
    return render_usage_overview(result, width=width)
