"""Interactive dashboard launcher composition."""

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.launch import ExecveDashboardProcess
from sidekick_usages.cli.dashboard.models.runtime import DashboardRuntime
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.paths import ApplicationPaths, discover_application_paths


def compose_dashboard_runtime(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
) -> DashboardRuntime:
    """Compose only passive cache and process-image boundaries."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_clock = SystemClock() if clock is None else clock
    return DashboardRuntime(
        CachedDashboardSnapshotSource(resolved_paths, resolved_clock),
        ExecveDashboardProcess(),
    )
