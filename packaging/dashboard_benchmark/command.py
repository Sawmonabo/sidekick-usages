"""Shared dashboard benchmark command execution."""

import sys
from collections.abc import Callable

from dashboard_benchmark.errors import DashboardBenchmarkError


def execute(command: Callable[[], int]) -> None:
    """Run one benchmark command with concise release diagnostics."""
    try:
        exit_code = command()
    except DashboardBenchmarkError as error:
        sys.stderr.write(f"dashboard benchmark failed: {error}\n")
        exit_code = 1
    sys.exit(exit_code)
