"""Cached dashboard frame rendering and terminal positioning."""

from rich.console import Console, RenderableType

from sidekick_usages.usage.dashboard.focus import initial_dashboard_cursor
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.overview import (
    dashboard_overview,
)

CURSOR_COLUMN_START = "\r"


def present_cached_dashboard(
    console: Console,
    snapshot: DashboardSnapshot,
) -> int:
    """Render and present one cached dashboard frame."""
    frame = render_dashboard_frame(
        console,
        dashboard_overview(
            snapshot,
            width=console.size.width,
            cursor=initial_dashboard_cursor(snapshot),
            footer=DashboardFooter(),
        ),
    )
    return present_dashboard_frame(console, frame)


def render_dashboard_frame(
    console: Console,
    renderable: RenderableType,
) -> str:
    """Render one cached frame through the invocation's Rich configuration."""
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def present_dashboard_frame(console: Console, frame: str) -> int:
    """Write one frame and rewind to its origin for process replacement."""
    line_count = frame.count("\n")
    console.file.write(frame)
    if line_count:
        console.file.write(f"\x1b[{line_count}A{CURSOR_COLUMN_START}")
    console.file.flush()
    return line_count


def restore_after_failed_replace(console: Console, line_count: int) -> None:
    """Move below the cached frame before rendering a launch failure."""
    if line_count:
        console.file.write(f"\x1b[{line_count}B{CURSOR_COLUMN_START}")
        console.file.flush()
