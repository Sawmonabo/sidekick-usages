"""Supervisor health fixtures."""

from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
)
from sidekick_usages.daemon.types.service import PackageVersion


def make_supervisor_health(
    *,
    queue: ServiceComponentState = ServiceComponentState.HEALTHY,
) -> SupervisorHealth:
    """Return one synthetic current supervisor observation."""
    return SupervisorHealth(
        backend=ServiceBackendId.SYSTEMD,
        cli_version=PackageVersion("0.7.0"),
        supervisor_version=PackageVersion("0.7.0"),
        platform=ServiceComponentState.HEALTHY,
        process=ServiceComponentState.HEALTHY,
        rescue=ServiceComponentState.NOT_REQUIRED,
        socket=ServiceComponentState.HEALTHY,
        peer=ServiceComponentState.HEALTHY,
        protocol=ServiceComponentState.HEALTHY,
        queue=queue,
        journal=ServiceComponentState.HEALTHY,
        broker=ServiceComponentState.NOT_REQUIRED,
    )
