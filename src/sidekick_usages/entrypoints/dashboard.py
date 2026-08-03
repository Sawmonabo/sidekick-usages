"""Dedicated interactive dashboard process image."""

import os
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.application import (
    InteractiveDashboardApplication,
)
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardApplicationResult,
)
from sidekick_usages.cli.dashboard.session import (
    InteractiveDashboardSession,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.cli.runtime.routing import parse_dashboard_arguments
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.credentials.capabilities.service import (
    build_provider_capability_service,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.lifecycle.manager import build_daemon_manager
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.lookup.store import (
    MetricsRefreshObservationRecorder,
    MetricsRefreshObservationStore,
)
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.providers.claude.managed.executable import (
    resolve_claude_launcher,
)
from sidekick_usages.providers.codex.app_server.executable import (
    resolve_codex_launcher,
)
from sidekick_usages.usage.lookup.worker.client import (
    UnavailableUsageLookupWorker,
    UsageLookupLaunchError,
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
    resolve_usage_lookup_interpreter,
)

INVALID_INVOCATION_EXIT_CODE = 2


def _connect_dashboard_control(socket_path: Path) -> ControlClient:
    """Open one bounded local supervisor connection."""
    return ControlClient.connect(socket_path)


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
    capabilities = build_provider_capability_service(paths, os.environ)
    try:
        lookup = UsageLookupWorkerClient(
            UsageLookupModuleLaunchPlanner(
                resolve_usage_lookup_interpreter(),
                os.environ,
            )
        )
    except UsageLookupLaunchError as error:
        lookup = UnavailableUsageLookupWorker(error.failure)
    session = InteractiveDashboardSession(
        snapshots.load(only),
        snapshots=snapshots,
        only=only,
        lookup=lookup,
        metrics_refresh=MetricsRefreshObservationRecorder(
            MetricsRefreshObservationStore(paths.metrics_refresh_status),
            clock,
        ),
        connector=_connect_dashboard_control,
        socket_path=paths.supervisor_socket,
        setup=GuidedServiceSetup(
            build_daemon_manager(
                claude_launcher=partial(
                    resolve_claude_launcher,
                    os.environ,
                ),
                codex_launcher=partial(
                    resolve_codex_launcher,
                    os.environ,
                ),
                paths=paths,
                clock=clock,
                provider_readiness=capabilities,
            ),
            ServiceSetupAcknowledgementStore(
                paths.service_setup_acknowledgement
            ),
        ),
    )
    return InteractiveDashboardApplication(session).run()


if __name__ == "__main__":
    sys.exit(main())
