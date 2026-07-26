"""Lean terminal and process-image boundary for the dashboard."""

import os
import sys
from pathlib import Path

from rich.console import Console, RenderableType

from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError

DASHBOARD_ENTRYPOINT_MODULE = "sidekick_usages.entrypoints.dashboard"
CURSOR_COLUMN_START = "\r"


class InteractiveDashboardLaunchError(UsageError):
    """The dedicated interactive process could not replace the launcher."""


class ExecveDashboardProcess:
    """Replace the launcher with the same absolute Python executable."""

    def replace(self, only: ProviderId | None) -> None:
        """Execute the dedicated dashboard module with safe routing state."""
        executable = Path(sys.executable).resolve(strict=True)
        arguments = [
            str(executable),
            "-m",
            DASHBOARD_ENTRYPOINT_MODULE,
        ]
        if only is not None:
            arguments.extend(("--only", only.value))
        os.execve(executable, arguments, os.environ.copy())


def interactive_dashboard_supported() -> bool:
    """Return whether this process owns both required Unix terminal streams."""
    return (
        sys.platform != "win32" and sys.stdin.isatty() and sys.stdout.isatty()
    )


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
