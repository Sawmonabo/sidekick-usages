"""Narrow-terminal per-account usage presentation."""

from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.models import AccountUsage
from sidekick_usages.usage.presentation.reset import reset_text

BAR_WIDTH = 18

_PCT_RED_THRESHOLD = 90
_PCT_YELLOW_THRESHOLD = 70
_PCT_CYAN_THRESHOLD = 40

PLAN_COLORS: dict[str, str] = {
    "max": "magenta",
    "team": "cyan",
    "pro": "green",
    "plus": "green",
    "enterprise": "yellow",
    "business": "yellow",
}


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
) -> Text:
    """Render a standalone account label and provider-plan tag."""
    header = Text(label, style="bold")
    header.append("  ")
    header.append_text(_account_tag(provider_id, plan))
    return header


def usage_report(
    usage: AccountUsage,
    reference_time: datetime,
) -> RenderableType:
    """Render one complete narrow-terminal account usage block."""
    windows = usage.report.active_windows()
    if not windows:
        return Group(
            account_header(usage.label, usage.provider_id, usage.plan),
            Text("  No active usage windows reported.", style="dim"),
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
    return Group(
        account_header(usage.label, usage.provider_id, usage.plan),
        table,
    )


__all__ = ["PLAN_COLORS", "account_header", "usage_report"]
