"""Typed semantic terminal text for dashboard layout and ANSI rendering."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.usage.presentation.formatting import (
    sanitize_terminal_text,
)


class DashboardTextStyle(StrEnum):
    """Closed visual roles used by dashboard text segments."""

    PLAIN = "plain"
    DIM = "dim"
    ROBOT = "robot"
    TITLE = "title"
    PRODUCT = "product"
    DESCRIPTION = "description"
    PROMISE = "promise"
    CLAUDE = "claude"
    CODEX = "codex"
    CLAUDE_TITLE = "claude_title"
    CODEX_TITLE = "codex_title"
    HEADER = "header"
    LABEL = "label"
    PLAN_MAX = "plan_max"
    PLAN_TEAM = "plan_team"
    PLAN_GREEN = "plan_green"
    PLAN_YELLOW = "plan_yellow"
    PLAN_DIM = "plan_dim"
    CURSOR = "cursor"
    WARNING = "warning"
    RESET = "reset"
    HEAT_ZERO = "heat_zero"
    HEAT_GREEN = "heat_green"
    HEAT_CYAN = "heat_cyan"
    HEAT_YELLOW = "heat_yellow"
    HEAT_RED = "heat_red"
    FOOTER_KEYS = "footer_keys"
    FOOTER_HELP = "footer_help"
    FOOTER_PROGRESS = "footer_progress"
    FOOTER_CONFIRMATION = "footer_confirmation"
    FOOTER_ERROR = "footer_error"


@dataclass(frozen=True, slots=True)
class DashboardText:
    """One sanitized text segment with one semantic visual role."""

    value: str
    style: DashboardTextStyle = DashboardTextStyle.PLAIN

    def __post_init__(self) -> None:
        """Reject terminal controls at the final text-model boundary."""
        if self.value != sanitize_terminal_text(self.value):
            raise ValueError("Dashboard text cannot contain controls.")


@dataclass(frozen=True, slots=True)
class DashboardLine:
    """One terminal line composed from ordered semantic text."""

    segments: tuple[DashboardText, ...] = ()

    @property
    def plain(self) -> str:
        """Return the exact unstyled terminal text."""
        return "".join(segment.value for segment in self.segments)
