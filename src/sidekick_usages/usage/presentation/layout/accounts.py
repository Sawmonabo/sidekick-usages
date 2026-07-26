"""Shared account-row layout primitives."""

from rich.text import Text

from sidekick_usages.branding.rich import PROVIDER_COLORS
from sidekick_usages.core.types import ProviderId

PLAN_COLORS: dict[str, str] = {
    "max": "magenta",
    "team": "cyan",
    "pro": "green",
    "plus": "green",
    "enterprise": "yellow",
    "business": "yellow",
}


def account_dot(provider_id: ProviderId) -> Text:
    """Render one provider-colored account bullet."""
    return Text("●", style=PROVIDER_COLORS.get(provider_id, "dim"))


def plan_text(plan: str) -> Text:
    """Render a plan chip, suppressing empty and unknown values."""
    if not plan or plan == "unknown":
        return Text("")
    return Text(plan, style=PLAN_COLORS.get(plan, "grey42"))


def account_tag(provider_id: ProviderId, plan: str) -> Text:
    """Render one compact provider and plan tag."""
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
    header.append_text(account_tag(provider_id, plan))
    return header
