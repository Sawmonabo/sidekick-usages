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


@dataclass(frozen=True, slots=True)
class TerminalDimensions:
    """One positive terminal viewport measured in rows and columns."""

    columns: int
    rows: int

    def __post_init__(self) -> None:
        """Reject dimensions that cannot represent a terminal viewport."""
        if self.columns < 1 or self.rows < 1:
            raise ValueError("Terminal dimensions must be positive.")


@dataclass(frozen=True, slots=True)
class DashboardRenderLayout:
    """Independent dashboard fragments for one terminal viewport."""

    masthead: str
    body: str
    status: str
    keys: str
    focused_body_line: int | None

    def __post_init__(self) -> None:
        """Reject a focused cursor outside the rendered body."""
        if self.focused_body_line is not None and self.focused_body_line < 0:
            raise ValueError("Focused dashboard body line cannot be negative.")
