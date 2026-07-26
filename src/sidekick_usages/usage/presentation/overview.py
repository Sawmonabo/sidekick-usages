"""Stable one-shot usage overview facade."""

from rich.console import RenderableType

from sidekick_usages.usage.models import UsageCheckResult
from sidekick_usages.usage.presentation.dashboard.overview import (
    render_usage_overview,
)


def usage_overview(
    result: UsageCheckResult,
    *,
    width: int,
) -> RenderableType:
    """Render the unchanged one-shot usage dashboard."""
    return render_usage_overview(result, width=width)
