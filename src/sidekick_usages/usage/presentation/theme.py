"""Dependency-free canonical usage-presentation theme."""

from sidekick_usages.branding.models import BrandTextRole, TerminalStyle
from sidekick_usages.branding.theme import (
    BRAND_STYLES,
    PROVIDER_COLORS,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.roles import UsageTextRole

ACTIVE_PERCENT_THRESHOLD = 1
CYAN_PERCENT_THRESHOLD = 40
YELLOW_PERCENT_THRESHOLD = 70
RED_PERCENT_THRESHOLD = 90
DIM_STYLE = TerminalStyle(dim=True)
ACCOUNT_LABEL_STYLE = TerminalStyle(foreground="#dadada")
PANEL_META_STYLE = TerminalStyle(foreground="#8a8a8a")
HEADER_STYLE = TerminalStyle(foreground="#6c6c6c")
MODEL_CAPTION_STYLE = TerminalStyle(foreground="#767676")
ACTIVITY_SINCE_STYLE = TerminalStyle(foreground="#585858")
MODEL_RULE_STYLE = TerminalStyle(foreground="#356f78")
HELP_STYLE = TerminalStyle(foreground="#b2b2b2")
ADVISORY_STYLE = TerminalStyle(foreground="#b59a55", dim=True)
CLAUDE_TITLE_STYLE = TerminalStyle(
    foreground=PROVIDER_COLORS[ProviderId.CLAUDE],
    bold=True,
)
CODEX_TITLE_STYLE = TerminalStyle(
    foreground=PROVIDER_COLORS[ProviderId.CODEX],
    bold=True,
)
PLAN_MAX_STYLE = TerminalStyle(foreground="magenta")
PLAN_TEAM_STYLE = TerminalStyle(foreground="cyan")
PLAN_GREEN_STYLE = TerminalStyle(foreground="green")
PLAN_YELLOW_STYLE = TerminalStyle(foreground="yellow")
PLAN_DIM_STYLE = DIM_STYLE
PLAN_STYLES: dict[str, TerminalStyle] = {
    "max": PLAN_MAX_STYLE,
    "team": PLAN_TEAM_STYLE,
    "pro": PLAN_GREEN_STYLE,
    "plus": PLAN_GREEN_STYLE,
    "enterprise": PLAN_YELLOW_STYLE,
    "business": PLAN_YELLOW_STYLE,
}
CURSOR_STYLE = TerminalStyle(foreground="cyan", bold=True)
HEAT_ZERO_STYLE = TerminalStyle(
    foreground="#cdd3d8",
    background="#353a40",
)
HEAT_GREEN_STYLE = TerminalStyle(
    foreground="#dfffe9",
    background="#1d5e35",
)
HEAT_CYAN_STYLE = TerminalStyle(
    foreground="#e2fbff",
    background="#1b6a87",
)
HEAT_YELLOW_STYLE = TerminalStyle(
    foreground="#fff4e0",
    background="#9c6f12",
)
HEAT_RED_STYLE = TerminalStyle(
    foreground="#ffe6e6",
    background="#b03030",
)
FOOTER_PROGRESS_STYLE = TerminalStyle(foreground="cyan")
FOOTER_CONFIRMATION_STYLE = TerminalStyle(foreground="yellow")
FOOTER_ERROR_STYLE = TerminalStyle(foreground="red")
USAGE_STYLES: dict[UsageTextRole, TerminalStyle] = {
    UsageTextRole.PLAIN: BRAND_STYLES[BrandTextRole.PLAIN],
    UsageTextRole.DIM: DIM_STYLE,
    UsageTextRole.ROBOT: BRAND_STYLES[BrandTextRole.ROBOT],
    UsageTextRole.TITLE: BRAND_STYLES[BrandTextRole.TITLE],
    UsageTextRole.PRODUCT: BRAND_STYLES[BrandTextRole.PRODUCT],
    UsageTextRole.DESCRIPTION: BRAND_STYLES[BrandTextRole.DESCRIPTION],
    UsageTextRole.PROMISE: BRAND_STYLES[BrandTextRole.PROMISE],
    UsageTextRole.CLAUDE: BRAND_STYLES[BrandTextRole.CLAUDE],
    UsageTextRole.CODEX: BRAND_STYLES[BrandTextRole.CODEX],
    UsageTextRole.CLAUDE_TITLE: CLAUDE_TITLE_STYLE,
    UsageTextRole.CODEX_TITLE: CODEX_TITLE_STYLE,
    UsageTextRole.MASTHEAD_DIVIDER: BRAND_STYLES[BrandTextRole.DIVIDER],
    UsageTextRole.HEADER: HEADER_STYLE,
    UsageTextRole.ACCOUNT_LABEL: ACCOUNT_LABEL_STYLE,
    UsageTextRole.PANEL_META: PANEL_META_STYLE,
    UsageTextRole.MODEL_CAPTION: MODEL_CAPTION_STYLE,
    UsageTextRole.ACTIVITY_SINCE: ACTIVITY_SINCE_STYLE,
    UsageTextRole.MODEL_RULE: MODEL_RULE_STYLE,
    UsageTextRole.PLAN_MAX: PLAN_MAX_STYLE,
    UsageTextRole.PLAN_TEAM: PLAN_TEAM_STYLE,
    UsageTextRole.PLAN_GREEN: PLAN_GREEN_STYLE,
    UsageTextRole.PLAN_YELLOW: PLAN_YELLOW_STYLE,
    UsageTextRole.PLAN_DIM: DIM_STYLE,
    UsageTextRole.CURSOR: CURSOR_STYLE,
    UsageTextRole.ADVISORY: ADVISORY_STYLE,
    UsageTextRole.RESET: HEADER_STYLE,
    UsageTextRole.LEGEND: HEADER_STYLE,
    UsageTextRole.HEAT_ZERO: HEAT_ZERO_STYLE,
    UsageTextRole.HEAT_GREEN: HEAT_GREEN_STYLE,
    UsageTextRole.HEAT_CYAN: HEAT_CYAN_STYLE,
    UsageTextRole.HEAT_YELLOW: HEAT_YELLOW_STYLE,
    UsageTextRole.HEAT_RED: HEAT_RED_STYLE,
    UsageTextRole.FOOTER_KEYS: PANEL_META_STYLE,
    UsageTextRole.FOOTER_HELP: HELP_STYLE,
    UsageTextRole.FOOTER_PROGRESS: FOOTER_PROGRESS_STYLE,
    UsageTextRole.FOOTER_CONFIRMATION: FOOTER_CONFIRMATION_STYLE,
    UsageTextRole.FOOTER_ERROR: FOOTER_ERROR_STYLE,
}


def usage_style(role: UsageTextRole) -> TerminalStyle:
    """Return the canonical terminal style for one semantic role."""
    return USAGE_STYLES[role]


def heat_role(percent: int | float) -> UsageTextRole:
    """Return the canonical utilization role for one percentage."""
    if percent >= RED_PERCENT_THRESHOLD:
        return UsageTextRole.HEAT_RED
    if percent >= YELLOW_PERCENT_THRESHOLD:
        return UsageTextRole.HEAT_YELLOW
    if percent >= CYAN_PERCENT_THRESHOLD:
        return UsageTextRole.HEAT_CYAN
    if percent >= ACTIVE_PERCENT_THRESHOLD:
        return UsageTextRole.HEAT_GREEN
    return UsageTextRole.HEAT_ZERO


def plan_role(plan: str) -> UsageTextRole:
    """Return the canonical plan-chip role."""
    normalized = plan.casefold()
    if normalized == "max":
        return UsageTextRole.PLAN_MAX
    if normalized == "team":
        return UsageTextRole.PLAN_TEAM
    if normalized in {"pro", "plus"}:
        return UsageTextRole.PLAN_GREEN
    if normalized in {"enterprise", "business"}:
        return UsageTextRole.PLAN_YELLOW
    return UsageTextRole.PLAN_DIM


def provider_role(provider_id: ProviderId) -> UsageTextRole:
    """Return one provider's canonical color role."""
    if provider_id is ProviderId.CLAUDE:
        return UsageTextRole.CLAUDE
    return UsageTextRole.CODEX


def provider_title_role(provider_id: ProviderId) -> UsageTextRole:
    """Return one provider title's canonical emphasized role."""
    if provider_id is ProviderId.CLAUDE:
        return UsageTextRole.CLAUDE_TITLE
    return UsageTextRole.CODEX_TITLE
