"""Dedicated interactive dashboard process image."""

import sys
from collections.abc import Sequence
from functools import partial
from threading import Lock

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.application import (
    InteractiveDashboardApplication,
)
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardApplicationResult,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardActionOwner,
    DashboardActionSink,
    DashboardLookupOwner,
    DashboardLookupSink,
    DashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.session import (
    InteractiveDashboardSession,
)
from sidekick_usages.cli.runtime.routing import parse_dashboard_arguments
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.paths import ApplicationPaths, discover_application_paths

INVALID_INVOCATION_EXIT_CODE = 2


def _build_dashboard_runtime(
    paths: ApplicationPaths,
    clock: Clock,
    snapshots: DashboardSnapshotSource,
    only: ProviderId | None,
    *,
    action_sink: DashboardActionSink,
    lookup_sink: DashboardLookupSink,
    snapshot_lock: Lock,
) -> tuple[DashboardActionOwner, DashboardLookupOwner]:
    """Build both dashboard runtime owners after the first paint."""
    from sidekick_usages.cli.dashboard import (
        composition,
    )

    return composition.build_dashboard_session_runtime(
        paths,
        clock,
        snapshots,
        only,
        action_sink=action_sink,
        lookup_sink=lookup_sink,
        snapshot_lock=snapshot_lock,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private interactive entry point on supported Unix platforms."""
    if sys.platform == "win32":
        return int(ExitCode.MANUAL_ACTION)
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        only = parse_dashboard_arguments(arguments)
    except ValueError:
        return INVALID_INVOCATION_EXIT_CODE
    paths = discover_application_paths()
    clock = SystemClock()
    return _run_dashboard_once(paths, clock, only)


def _run_dashboard_once(
    paths: ApplicationPaths,
    clock: Clock,
    only: ProviderId | None,
) -> DashboardApplicationResult:
    """Build and run one fresh dashboard session."""
    snapshots = CachedDashboardSnapshotSource(paths, clock)
    session = InteractiveDashboardSession(
        snapshots.load(only),
        snapshots=snapshots,
        only=only,
        runtime=partial(
            _build_dashboard_runtime,
            paths,
            clock,
            snapshots,
            only,
        ),
    )
    return InteractiveDashboardApplication(session).run()


if __name__ == "__main__":
    sys.exit(main())
