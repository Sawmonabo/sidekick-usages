"""Durable guided service-setup acknowledgement scenario."""

from pathlib import Path

from sidekick_usages.cli.dashboard.models.controller import DashboardIntent
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.usage.dashboard.models import DashboardService
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
