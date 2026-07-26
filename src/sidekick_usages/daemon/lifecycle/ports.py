"""Structural ports for user-service lifecycle composition."""

from collections.abc import Callable
from typing import Protocol

from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.lifecycle import (
    ServiceBackendStatus,
    ServiceLifecycleObservation,
    SupervisorHealth,
)
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceBackendId,
)

type ServiceLifecycleObserver = Callable[
    [ServiceLifecycleObservation],
    None,
]


def discard_service_lifecycle_observation(
    _observation: ServiceLifecycleObservation,
) -> None:
    """Discard optional lifecycle progress."""


class ServiceBackend(Protocol):
    """One complete operating-system user-service integration."""

    id: ServiceBackendId

    def cancel(self) -> None:
        """Interrupt one active native lifecycle command."""

    def install(self, progress: ServiceLifecycleObserver, /) -> None:
        """Install and start the resident service."""

    def restart(self, progress: ServiceLifecycleObserver, /) -> None:
        """Restart the installed resident service."""

    def status(self) -> ServiceBackendStatus:
        """Return safe installation and runtime state."""

    def uninstall(self) -> None:
        """Stop and remove the resident service integration."""


class ServiceReadiness(Protocol):
    """Prepare and verify supervisor readiness."""

    def cancel(self) -> None:
        """Interrupt active readiness observation."""

    def enroll_accounts(self) -> None:
        """Persist one scheduled maintenance slot per saved account."""

    def verify_ready(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver,
    ) -> None:
        """Verify protocol, state, queue, and broker capability."""

    def wait_until_ready(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver,
    ) -> None:
        """Wait for bounded resident startup, then verify readiness."""

    def complete_maintenance_pass(
        self,
        progress: ServiceLifecycleObserver,
    ) -> None:
        """Complete or truthfully settle one bounded readiness pass."""

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        """Inspect each resident-service component without mutation."""


class ProviderCapabilityReadiness(Protocol):
    """Read-only provider capability evidence consumed by readiness."""

    def cancel(self) -> None:
        """Interrupt any cancellable provider capability probe."""

    def ready(self, provider_id: ProviderId) -> bool:
        """Return whether the authoritative provider gate passed."""


class ServiceCleanup(Protocol):
    """Remove only service-owned transient state."""

    def clear(self) -> None:
        """Remove service state while preserving user and provider state."""
