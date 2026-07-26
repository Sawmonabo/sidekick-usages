"""Cached-first public CLI runtime and closed process-image boundary."""

import os
import sys
from collections.abc import Sequence
from pathlib import Path

if sys.platform == "win32":
    import subprocess

from rich.console import Console

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard import launch
from sidekick_usages.cli.runtime.routing import (
    dashboard_arguments,
    parse_dashboard_arguments,
)
from sidekick_usages.clock import SystemClock
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.errors import (
    PersistenceError,
    exit_code_for_persistence_code,
)
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable

APPLICATION_MODULE = "sidekick_usages.cli.runtime.application"
INTERACTIVE_DASHBOARD_MODULE = "sidekick_usages.entrypoints.dashboard"
PYTHON_IO_ENCODING_ENVIRONMENT_KEY = "PYTHONIOENCODING"
UTF8_IO_ENCODING = "utf-8"
PROCESS_LAUNCH_FAILURE_MESSAGE = "Sidekick could not start the requested CLI."
PROCESS_LAUNCH_FAILURE_EXIT_CODE = 2


def execute_application(arguments: Sequence[str]) -> int:
    """Enter the complete Typer application process image."""
    return _execute_module(APPLICATION_MODULE, arguments)


def execute_interactive_dashboard(arguments: Sequence[str]) -> int:
    """Enter the isolated interactive-dashboard process image."""
    return _execute_module(INTERACTIVE_DASHBOARD_MODULE, arguments)


def main(argv: Sequence[str] | None = None) -> int:
    """Paint cached TTY state without starting a second interpreter."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        if not _interactive_terminal_supported():
            return execute_application(arguments)
        try:
            only = parse_dashboard_arguments(arguments)
        except ValueError:
            return execute_application(arguments)
        return _run_cached_dashboard(only)
    except ExecutableQualificationError, OSError, ValueError:
        sys.stderr.write(f"{PROCESS_LAUNCH_FAILURE_MESSAGE}\n")
        return PROCESS_LAUNCH_FAILURE_EXIT_CODE


def _run_cached_dashboard(only: ProviderId | None) -> int:
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


def _interactive_terminal_supported() -> bool:
    return (
        sys.platform != "win32" and sys.stdin.isatty() and sys.stdout.isatty()
    )


def _execute_module(module: str, arguments: Sequence[str]) -> int:
    if (
        not module
        or "\0" in module
        or any("\0" in value for value in arguments)
    ):
        raise ValueError("Process-image arguments are invalid.")
    executable = _qualified_interpreter()
    command = (str(executable), "-m", module, *arguments)
    environment = os.environ.copy()
    if sys.platform == "win32":
        environment[PYTHON_IO_ENCODING_ENVIRONMENT_KEY] = UTF8_IO_ENCODING
        return subprocess.run(
            command,
            check=False,
            env=environment,
        ).returncode
    return os.execve(executable, command, environment)


def _qualified_interpreter() -> Path:
    executable = Path(sys.executable)
    qualify_executable(executable)
    return executable


if __name__ == "__main__":
    sys.exit(main())
