"""Typed semantic terminal text for dashboard layout and ANSI rendering."""

from dataclasses import dataclass

from sidekick_usages.usage.presentation.formatting import (
    sanitize_terminal_text,
)
from sidekick_usages.usage.presentation.theme import UsageTextRole


@dataclass(frozen=True, slots=True)
class DashboardText:
    """One sanitized text segment with one semantic visual role."""

    value: str
    style: UsageTextRole = UsageTextRole.PLAIN

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
