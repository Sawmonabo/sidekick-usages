"""Bounded keyboard and progress footers for the interactive dashboard."""

from rich.console import RenderableType
from rich.text import Text

from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardFooterKind,
)

KEY_FOOTER = (
    " ↑/↓ or j/k move   Tab provider   Enter use   r refresh   ? help   q exit"
)
HELP_FOOTER = (
    " ↑/↓ or j/k move   Tab provider   Enter use   Esc cancel   "
    "r refresh   R refresh all   ? close help   q exit"
)
FOOTER_STYLES: dict[DashboardFooterKind, str] = {
    DashboardFooterKind.PROGRESS: "cyan",
    DashboardFooterKind.CONFIRMATION: "yellow",
    DashboardFooterKind.ERROR: "red",
}


def footer_renderable(footer: DashboardFooter) -> RenderableType:
    """Render one bounded footer without attaching state to account rows."""
    if footer.kind in {
        DashboardFooterKind.PROGRESS,
        DashboardFooterKind.CONFIRMATION,
        DashboardFooterKind.ERROR,
    }:
        if footer.message is None:
            raise ValueError("Transient footer requires a message.")
        return Text(
            f" {footer.message}",
            style=FOOTER_STYLES[footer.kind],
        )
    if footer.kind is DashboardFooterKind.HELP:
        return Text(HELP_FOOTER, style="grey70")
    return Text(KEY_FOOTER, style="grey54")
