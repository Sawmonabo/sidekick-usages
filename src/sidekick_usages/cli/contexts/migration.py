"""Managed-auth migration context composition."""

from sidekick_usages.credentials.migration.models.service import (
    ManagedAuthServiceResult,
)
from sidekick_usages.credentials.migration.types.service import (
    ManagedAuthServiceState,
)
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.models.lifecycle import DaemonOperationResult
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState

SERVICE_MIGRATION_STATES = {
    ServiceLifecycleState.ABSENT: ManagedAuthServiceState.INSTALL_REQUIRED,
    ServiceLifecycleState.INSTALLED: ManagedAuthServiceState.RESTART_REQUIRED,
    ServiceLifecycleState.READY: ManagedAuthServiceState.READY,
    ServiceLifecycleState.UNHEALTHY: ManagedAuthServiceState.RESTART_REQUIRED,
    ServiceLifecycleState.FEATURE_DISABLED: ManagedAuthServiceState.BLOCKED,
}


class ManagedAuthDaemonLifecycle:
    """Adapt the resident daemon to the credential migration capability."""

    def __init__(self, daemon: DaemonManager) -> None:
        self._daemon = daemon

    def status(self) -> ManagedAuthServiceResult:
        """Return provider-neutral current service readiness."""
        return _migration_result(self._daemon.status())

    def install(self) -> ManagedAuthServiceResult:
        """Install and verify the current user-level service."""
        return _migration_result(self._daemon.install())

    def restart(self) -> ManagedAuthServiceResult:
        """Restart and verify the current user-level service."""
        return _migration_result(self._daemon.restart())


def _migration_result(
    result: DaemonOperationResult,
) -> ManagedAuthServiceResult:
    """Translate one daemon result at the CLI composition boundary."""
    return ManagedAuthServiceResult(
        state=SERVICE_MIGRATION_STATES[result.state],
        message=result.message,
        exit_code=result.exit_code,
    )
