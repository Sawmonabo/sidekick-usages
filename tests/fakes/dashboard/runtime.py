"""Reusable dashboard routing and setup fakes."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.lifecycle.ports import (
    ServiceLifecycleObserver,
    discard_service_lifecycle_observation,
)
from sidekick_usages.daemon.models.lifecycle import (
    DaemonOperationResult,
    ServiceLifecycleObservation,
)
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecyclePhase,
    ServiceLifecycleState,
)

EXPECTED_SERVICE_SETUP_PROGRESS = frozenset(
    {
        "Installing the Sidekick user service.",
        "Starting the Sidekick user service.",
        "Verifying the Sidekick control socket.",
        "Verifying durable account-maintenance recovery.",
        "Verifying Claude CLI capabilities.",
        "Verifying the initial account-maintenance pass.",
        "Restarting the Sidekick user service.",
        "Verifying the Codex account broker.",
        "Verifying Codex CLI capabilities.",
    }
)


class OneShotRecorder:
    """Record stable one-shot routing without composing providers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _ctx: object) -> None:
        """Record one existing workflow dispatch."""
        self.calls += 1


class SetupDaemon(DaemonManager):
    """Record guided setup without opening platform boundaries."""

    def __init__(
        self,
        state: ServiceLifecycleState,
        *,
        provider_ready: bool = True,
    ) -> None:
        self.state = state
        self.provider_ready = provider_ready
        self.events: list[str] = []
        self.cancelled = False

    def cancel(self) -> None:
        """Record dashboard lifecycle cancellation."""
        self.cancelled = True

    def status(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Record one current service check."""
        self.events.append(_setup_event("status", provider_ids))
        if self.state is ServiceLifecycleState.READY:
            _publish_readiness(provider_ids, progress)
        return self._result(provider_ids)

    def restart(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Record one bounded restart."""
        self.events.append(_setup_event("restart", provider_ids))
        self.state = ServiceLifecycleState.READY
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.RESTARTING))
        _publish_readiness(provider_ids, progress)
        return self._result(provider_ids)

    def install(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Record one approved user-level installation."""
        self.events.append(_setup_event("install", provider_ids))
        self.state = ServiceLifecycleState.READY
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.INSTALLING))
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.STARTING))
        _publish_readiness(provider_ids, progress)
        result = self._result(provider_ids)
        if result.state is not ServiceLifecycleState.READY:
            return result
        progress(
            ServiceLifecycleObservation(
                ServiceLifecyclePhase.MAINTENANCE_COMPLETED
            )
        )
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.RESTARTING))
        _publish_readiness(provider_ids, progress)
        return result

    def _result(
        self,
        provider_ids: ProviderReadinessScope,
    ) -> DaemonOperationResult:
        provider_failure = (
            self.state is ServiceLifecycleState.READY
            and bool(provider_ids)
            and not self.provider_ready
        )
        return DaemonOperationResult(
            ServiceBackendId.SYSTEMD,
            (
                ServiceLifecycleState.UNHEALTHY
                if provider_failure
                else self.state
            ),
            "Synthetic user-service result.",
            failure_code=(
                ServiceFailureCode.PROVIDER_CAPABILITY_UNAVAILABLE
                if provider_failure
                else None
            ),
            failure_provider_id=provider_ids[0] if provider_failure else None,
        )


def _setup_event(
    operation: str,
    provider_ids: ProviderReadinessScope,
) -> str:
    if not provider_ids:
        return operation
    providers = "+".join(provider_id.value for provider_id in provider_ids)
    return f"{operation}:{providers}"


def _publish_readiness(
    provider_ids: ProviderReadinessScope,
    progress: ServiceLifecycleObserver,
) -> None:
    progress(ServiceLifecycleObservation(ServiceLifecyclePhase.CONTROL_SOCKET))
    progress(
        ServiceLifecycleObservation(ServiceLifecyclePhase.DURABLE_RECOVERY)
    )
    if ProviderId.CODEX in provider_ids:
        progress(
            ServiceLifecycleObservation(ServiceLifecyclePhase.CODEX_BROKER)
        )
    for provider_id in provider_ids:
        progress(
            ServiceLifecycleObservation(
                ServiceLifecyclePhase.PROVIDER_CAPABILITY,
                provider_id,
            )
        )


def interactive_terminal() -> bool:
    """Represent an interactive terminal."""
    return True


def redirected_terminal() -> bool:
    """Represent redirected input or output."""
    return False
