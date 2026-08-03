"""Post-paint interactive dashboard runtime composition."""

import os
from functools import partial
from pathlib import Path
from threading import Lock

from sidekick_usages.cli.dashboard.actions import DashboardActionExecutor
from sidekick_usages.cli.dashboard.lookup import (
    DashboardLookupCoordinator,
    DashboardLookupWorker,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardActionOwner,
    DashboardActionSink,
    DashboardLookupOwner,
    DashboardLookupSink,
    DashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.clock import Clock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.service import (
    build_provider_capability_service,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.lifecycle.manager import build_daemon_manager
from sidekick_usages.paths import ApplicationPaths
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


def _connect_dashboard_control(socket_path: Path) -> ControlClient:
    """Open one bounded local supervisor connection."""
    return ControlClient.connect(socket_path)


def _build_dashboard_lookup_worker() -> DashboardLookupWorker:
    """Build the isolated live-lookup worker."""
    try:
        return UsageLookupWorkerClient(
            UsageLookupModuleLaunchPlanner(
                resolve_usage_lookup_interpreter(),
                os.environ,
            )
        )
    except UsageLookupLaunchError as error:
        return UnavailableUsageLookupWorker(error.failure)


def build_dashboard_session_runtime(
    paths: ApplicationPaths,
    clock: Clock,
    snapshots: DashboardSnapshotSource,
    only: ProviderId | None,
    *,
    action_sink: DashboardActionSink,
    lookup_sink: DashboardLookupSink,
    snapshot_lock: Lock,
) -> tuple[DashboardActionOwner, DashboardLookupOwner]:
    """Build both runtime owners after the cached first frame."""
    capabilities = build_provider_capability_service(paths, os.environ)
    actions = DashboardActionExecutor(
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
        sink=action_sink,
    )
    lookup = DashboardLookupCoordinator(
        snapshots=snapshots,
        only=only,
        worker=_build_dashboard_lookup_worker(),
        metrics_refresh=MetricsRefreshObservationRecorder(
            MetricsRefreshObservationStore(paths.metrics_refresh_status),
            clock,
        ),
        snapshot_lock=snapshot_lock,
        sink=lookup_sink,
    )
    return actions, lookup
