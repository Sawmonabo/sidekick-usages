"""Canonical terminal geometry normalization."""

from sidekick_usages.usage.presentation.dashboard.render.models import (
    TerminalDimensions,
)


def terminal_dimensions(columns: int, rows: int) -> TerminalDimensions:
    """Normalize raw terminal rows and columns to positive dimensions."""
    return TerminalDimensions(
        columns=max(1, columns),
        rows=max(1, rows),
    )
