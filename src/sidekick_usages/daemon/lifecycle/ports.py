"""Structural ports for user-service lifecycle composition."""

from typing import Protocol

from sidekick_usages.daemon.models.lifecycle import (
    ServiceBackendStatus,
    SupervisorHealth,
)
from sidekick_usages.daemon.types.lifecycle import ServiceBackendId

__all__ = ["ServiceBackend", "ServiceCleanup", "ServiceReadiness"]


class ServiceBackend(Protocol):
    """One complete operating-system user-service integration."""

    id: ServiceBackendId

    def install(self) -> None:
        """Install and start the resident service."""

    def restart(self) -> None:
        """Restart the installed resident service."""

    def status(self) -> ServiceBackendStatus:
        """Return safe installation and runtime state."""

    def uninstall(self) -> None:
        """Stop and remove the resident service integration."""


class ServiceReadiness(Protocol):
    """Prepare and verify supervisor readiness."""

    def enroll_accounts(self) -> None:
        """Persist one scheduled maintenance slot per saved account."""

    def verify_ready(self) -> None:
        """Verify protocol, state, queue, and broker capability."""

    def complete_maintenance_pass(self) -> None:
        """Complete or truthfully settle one bounded readiness pass."""

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        """Inspect each resident-service component without mutation."""


class ServiceCleanup(Protocol):
    """Remove only service-owned transient state."""

    def clear(self) -> None:
        """Remove service state while preserving user and provider state."""
