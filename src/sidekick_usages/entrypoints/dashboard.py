"""Dedicated interactive dashboard process image."""

import sys
from collections.abc import Sequence

from sidekick_usages.cli.contexts.dashboard import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.application import (
    InteractiveDashboardApplication,
)
from sidekick_usages.clock import SystemClock
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.paths import discover_application_paths

INVALID_INVOCATION_EXIT_CODE = 2
ONLY_ARGUMENT_COUNT = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private interactive entry point on supported Unix platforms."""
    if sys.platform == "win32":
        return int(ExitCode.MANUAL_ACTION)
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        only = _parse_only(arguments)
    except ValueError:
        return INVALID_INVOCATION_EXIT_CODE
    snapshots = CachedDashboardSnapshotSource(
        discover_application_paths(),
        SystemClock(),
    )
    return InteractiveDashboardApplication(snapshots.load(only)).run()


def _parse_only(arguments: tuple[str, ...]) -> ProviderId | None:
    if not arguments:
        return None
    if len(arguments) == ONLY_ARGUMENT_COUNT and arguments[0] == "--only":
        return ProviderId(arguments[1])
    raise ValueError("Invalid private dashboard invocation.")


if __name__ == "__main__":
    sys.exit(main())
