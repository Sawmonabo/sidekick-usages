"""Lean cached-dashboard first-paint process image."""

import sys
from collections.abc import Sequence

from rich.console import Console

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard import launch
from sidekick_usages.cli.runtime.bootstrap import (
    PROCESS_LAUNCH_FAILURE_MESSAGE,
    execute_application,
    execute_interactive_dashboard,
)
from sidekick_usages.cli.runtime.routing import (
    dashboard_arguments,
    parse_dashboard_arguments,
)
from sidekick_usages.clock import SystemClock
from sidekick_usages.core.types import ExitCode
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.errors import (
    PersistenceError,
    exit_code_for_persistence_code,
)
from sidekick_usages.platform.errors import ExecutableQualificationError


def main(argv: Sequence[str] | None = None) -> int:
    """Paint cached state before entering the interactive process image."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        only = parse_dashboard_arguments(arguments)
    except ValueError:
        return execute_application(arguments)
    console = Console()
    try:
        snapshot = CachedDashboardSnapshotSource(
            discover_application_paths(),
            SystemClock(),
        ).load(only)
        line_count = launch.present_cached_dashboard(console, snapshot)
        try:
            return execute_interactive_dashboard(dashboard_arguments(only))
        except ExecutableQualificationError, OSError, ValueError:
            launch.restore_after_failed_replace(console, line_count)
            raise UsageError(PROCESS_LAUNCH_FAILURE_MESSAGE) from None
    except PersistenceError as error:
        Console(stderr=True).print(f"[red]{error}[/red]")
        return int(exit_code_for_persistence_code(error.code))
    except UsageError as error:
        Console(stderr=True).print(f"[red]{error}[/red]")
        return int(ExitCode.MANUAL_ACTION)


if __name__ == "__main__":
    sys.exit(main())
