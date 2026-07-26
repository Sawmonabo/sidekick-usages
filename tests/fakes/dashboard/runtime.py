"""Reusable dashboard routing and setup fakes."""

import io
from dataclasses import replace
from datetime import datetime

from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.models.lifecycle import DaemonOperationResult
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceBackendId,
    ServiceLifecycleState,
)
from sidekick_usages.usage.dashboard.models import DashboardSnapshot
from tests.fakes.dashboard.state import controller_snapshot


class RoutingSnapshotSource:
    """Record one provider-scoped cached read."""

    def __init__(
        self,
        events: list[str],
        reference_time: datetime,
    ) -> None:
        self._events = events
        self._reference_time = reference_time

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return the synthetic dashboard with the requested scope."""
        self._events.append(f"load:{only}")
        snapshot = controller_snapshot(self._reference_time)
        if only is None:
            return snapshot
        return replace(
            snapshot,
            providers=(
                replace(snapshot.providers[0], rows=()),
                snapshot.providers[1],
            ),
        )


class RoutingDashboardProcess:
    """Record replacement only after observing the cached frame."""

    def __init__(self, events: list[str], output: io.StringIO) -> None:
        self._events = events
        self._output = output
        self.frame_at_replace = ""

    def replace(self, only: ProviderId | None) -> None:
        """Capture the exact output visible at the replacement boundary."""
        self.frame_at_replace = self._output.getvalue()
        self._events.append(f"replace:{only}")


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
    ) -> DaemonOperationResult:
        """Record one current service check."""
        self.events.append(_setup_event("status", provider_ids))
        return self._result(self._provider_state(provider_ids))

    def restart(
        self,
        provider_ids: ProviderReadinessScope = (),
    ) -> DaemonOperationResult:
        """Record one bounded restart."""
        self.events.append(_setup_event("restart", provider_ids))
        self.state = ServiceLifecycleState.READY
        return self._result(self._provider_state(provider_ids))

    def install(
        self,
        provider_ids: ProviderReadinessScope = (),
    ) -> DaemonOperationResult:
        """Record one approved user-level installation."""
        self.events.append(_setup_event("install", provider_ids))
        self.state = ServiceLifecycleState.READY
        return self._result(self._provider_state(provider_ids))

    def _provider_state(
        self,
        provider_ids: ProviderReadinessScope,
    ) -> ServiceLifecycleState:
        if (
            self.state is ServiceLifecycleState.READY
            and provider_ids
            and not self.provider_ready
        ):
            return ServiceLifecycleState.UNHEALTHY
        return self.state

    @staticmethod
    def _result(state: ServiceLifecycleState) -> DaemonOperationResult:
        return DaemonOperationResult(
            ServiceBackendId.SYSTEMD,
            state,
            "Synthetic user-service result.",
        )


def _setup_event(
    operation: str,
    provider_ids: ProviderReadinessScope,
) -> str:
    if not provider_ids:
        return operation
    providers = "+".join(provider_id.value for provider_id in provider_ids)
    return f"{operation}:{providers}"


def interactive_terminal() -> bool:
    """Represent an interactive terminal."""
    return True


def redirected_terminal() -> bool:
    """Represent redirected input or output."""
    return False
