"""Shared account-row layout primitives."""

from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.branding.theme import PROVIDER_COLORS
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.theme import (
    ACCOUNT_LABEL_STYLE,
    PLAN_DIM_STYLE,
    PLAN_STYLES,
)


def account_dot(provider_id: ProviderId) -> Text:
    """Render one provider-colored account bullet."""
    return Text("●", style=PROVIDER_COLORS.get(provider_id, "dim"))


def plan_text(plan: str) -> Text:
    """Render a plan chip, suppressing empty and unknown values."""
    if not plan or plan == "unknown":
        return Text("")
    theme = PLAN_STYLES.get(plan.casefold(), PLAN_DIM_STYLE)
    return Text(plan, style=rich_style(theme))


def account_tag(provider_id: ProviderId, plan: str) -> Text:
    """Render one compact provider and plan tag."""
    provider_color = PROVIDER_COLORS.get(provider_id, "dim")
    tag = Text("[", style="dim")
    tag.append(provider_id, style=provider_color)
    if plan and plan != "unknown":
        tag.append(" · ", style="dim")
        theme = PLAN_STYLES.get(plan.casefold(), PLAN_DIM_STYLE)
        tag.append(plan, style=rich_style(theme))
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
    header.append(label, style=rich_style(ACCOUNT_LABEL_STYLE))
    header.append("  ")
    header.append_text(account_tag(provider_id, plan))
    return header
