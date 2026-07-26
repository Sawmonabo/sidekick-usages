"""Lean public CLI dispatcher and closed process-image boundary."""

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from sidekick_usages.cli.runtime.routing import dashboard_candidate
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable

APPLICATION_MODULE = "sidekick_usages.cli.runtime.application"
CACHED_DASHBOARD_MODULE = "sidekick_usages.cli.runtime.dashboard"
INTERACTIVE_DASHBOARD_MODULE = "sidekick_usages.entrypoints.dashboard"
PROCESS_LAUNCH_FAILURE_MESSAGE = "Sidekick could not start the requested CLI."
PROCESS_LAUNCH_FAILURE_EXIT_CODE = 2


def execute_application(arguments: Sequence[str]) -> int:
    """Enter the complete Typer application process image."""
    return _execute_module(APPLICATION_MODULE, arguments)


def execute_cached_dashboard(arguments: Sequence[str]) -> int:
    """Enter the passive cached-dashboard process image."""
    return _execute_module(CACHED_DASHBOARD_MODULE, arguments)


def execute_interactive_dashboard(arguments: Sequence[str]) -> int:
    """Enter the isolated interactive-dashboard process image."""
    return _execute_module(INTERACTIVE_DASHBOARD_MODULE, arguments)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch cached TTY startup without importing the Typer graph."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        if (
            _interactive_terminal_supported()
            and dashboard_candidate(arguments)
        ):
            return execute_cached_dashboard(arguments)
        return execute_application(arguments)
    except (ExecutableQualificationError, OSError, ValueError):
        sys.stderr.write(f"{PROCESS_LAUNCH_FAILURE_MESSAGE}\n")
        return PROCESS_LAUNCH_FAILURE_EXIT_CODE


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
