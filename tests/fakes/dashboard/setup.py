"""Durable guided service-setup acknowledgement scenario."""

from functools import partial
from pathlib import Path
from threading import Lock

from sidekick_usages.cli.dashboard.actions import DashboardActionExecutor
from sidekick_usages.cli.dashboard.lookup import DashboardLookupCoordinator
from sidekick_usages.cli.dashboard.models.controller import DashboardIntent
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardActionOwner,
    DashboardActionSink,
    DashboardControlConnector,
    DashboardLookupOwner,
    DashboardLookupSink,
    DashboardLookupWorker,
    DashboardSessionRuntimeFactory,
    DashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.usage.dashboard.models import DashboardService
from sidekick_usages.usage.lookup.diagnostics.ports import (
    MetricsRefreshObservationSink,
)
from tests.fakes.dashboard.runtime import SetupDaemon

type SetupAcknowledgementProof = tuple[
    bool,
    ServiceSetupOutcome,
    tuple[str, ...],
    ServiceSetupOutcome,
    tuple[str, ...],
    bool,
]


def guided_setup(
    daemon: DaemonManager,
    acknowledgement_path: Path,
) -> GuidedServiceSetup:
    """Build guided setup against one isolated acknowledgement authority."""
    return GuidedServiceSetup(
        daemon,
        ServiceSetupAcknowledgementStore(acknowledgement_path),
    )


def _build_dashboard_runtime(
    snapshots: DashboardSnapshotSource,
    only: ProviderId | None,
    lookup: DashboardLookupWorker,
    metrics_refresh: MetricsRefreshObservationSink,
    action_parts: tuple[
        DashboardControlConnector,
        Path,
        GuidedServiceSetup,
    ],
    *,
    action_sink: DashboardActionSink,
    lookup_sink: DashboardLookupSink,
    snapshot_lock: Lock,
) -> tuple[DashboardActionOwner, DashboardLookupOwner]:
    """Build both synthetic runtime owners."""
    connector, socket_path, setup = action_parts
    actions = DashboardActionExecutor(
        connector=connector,
        socket_path=socket_path,
        setup=setup,
        sink=action_sink,
    )
    lookups = DashboardLookupCoordinator(
        snapshots=snapshots,
        only=only,
        worker=lookup,
        metrics_refresh=metrics_refresh,
        snapshot_lock=snapshot_lock,
        sink=lookup_sink,
    )
    return actions, lookups


def dashboard_runtime(
    snapshots: DashboardSnapshotSource,
    only: ProviderId | None,
    lookup: DashboardLookupWorker,
    metrics_refresh: MetricsRefreshObservationSink,
    connector: DashboardControlConnector,
    socket_path: Path,
    setup: GuidedServiceSetup,
) -> DashboardSessionRuntimeFactory:
    """Delay both synthetic owners until session start."""
    return partial(
        _build_dashboard_runtime,
        snapshots,
        only,
        lookup,
        metrics_refresh,
        (connector, socket_path, setup),
    )


def exercise_setup_acknowledgement(
    acknowledgement_path: Path,
    *,
    service: DashboardService,
    intent: DashboardIntent,
) -> SetupAcknowledgementProof:
    """Exercise same-generation reuse and incompatible re-prompting."""
    repeated_daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    repeated = guided_setup(
        repeated_daemon,
        acknowledgement_path,
    ).prepare(
        service=service,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
    )

    acknowledgements = ServiceSetupAcknowledgementStore(acknowledgement_path)
    acknowledged = acknowledgements.matches(PROTOCOL_VERSION)
    acknowledgements.acknowledge(PROTOCOL_VERSION + 1)
    incompatible_daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    incompatible = guided_setup(
        incompatible_daemon,
        acknowledgement_path,
    ).prepare(
        service=service,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
    )
    acknowledgement_path.write_bytes(b"{}")
    try:
        acknowledgements.acknowledge(PROTOCOL_VERSION)
    except InvalidSchemaError:
        malformed_rejected = True
    else:
        malformed_rejected = False
    return (
        acknowledged,
        repeated.outcome,
        tuple(repeated_daemon.events),
        incompatible.outcome,
        tuple(incompatible_daemon.events),
        malformed_rejected,
    )
