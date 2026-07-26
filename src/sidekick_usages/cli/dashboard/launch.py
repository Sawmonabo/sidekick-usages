"""Cached dashboard frame rendering and terminal positioning."""

from typing import TextIO

from sidekick_usages.usage.dashboard.focus import initial_dashboard_cursor
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard,
)

CURSOR_COLUMN_START = "\r"


def present_cached_dashboard(
    output: TextIO,
    snapshot: DashboardSnapshot,
    *,
    width: int,
    color: bool,
) -> int:
    """Render and present one cached dashboard frame."""
    frame = render_dashboard(
        snapshot,
        width=width,
        cursor=initial_dashboard_cursor(snapshot),
        footer=DashboardFooter(),
        color=color,
    )
    return present_dashboard_frame(output, frame)


def present_dashboard_frame(output: TextIO, frame: str) -> int:
    """Write one frame and rewind to its origin for process replacement."""
    line_count = frame.count("\n")
    output.write(frame)
    if line_count:
        output.write(f"\x1b[{line_count}A{CURSOR_COLUMN_START}")
    output.flush()
    return line_count


def restore_after_failed_replace(output: TextIO, line_count: int) -> None:
    """Move below the cached frame before rendering a launch failure."""
    if line_count:
        output.write(f"\x1b[{line_count}B{CURSOR_COLUMN_START}")
        output.flush()
