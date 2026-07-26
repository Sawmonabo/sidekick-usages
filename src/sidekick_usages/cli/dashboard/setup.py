"""Guided per-user service readiness for pending dashboard actions."""

from collections.abc import Callable
from threading import Event
from typing import assert_never

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardIntent,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
    ServiceSetupProgress,
    ServiceSetupResult,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceLifecycleState,
)
from sidekick_usages.usage.dashboard.models import DashboardService

type ServiceSetupProgressSink = Callable[[ServiceSetupProgress], None]

_ALL_PROVIDER_IDS = tuple(ProviderId)

def _discard_progress(_progress: ServiceSetupProgress) -> None:
    """Discard optional setup progress."""


class GuidedServiceSetup:
    """Prepare the resident service before one pending dashboard action."""

    def __init__(self, daemon: DaemonManager) -> None:
        self._daemon = daemon
        self._closed = Event()

    def close(self) -> None:
        """Interrupt only dashboard-owned lifecycle observation."""
        self._closed.set()
        self._daemon.cancel()

    def prepare(
        self,
        *,
        service: DashboardService,
        intent: DashboardIntent,
        interactive: bool,
        decision: ServiceSetupDecision,
        progress: ServiceSetupProgressSink = _discard_progress,
    ) -> ServiceSetupResult[DashboardIntent]:
        """Return the original intent only after bounded service readiness."""
        provider_ids = _provider_ids(intent)
        progress(ServiceSetupProgress.CHECKING)
        status = self._daemon.status(provider_ids)
        if self._closed.is_set():
            return self._failed(intent)
        if status.state is ServiceLifecycleState.FEATURE_DISABLED:
            return ServiceSetupResult(
                intent=intent,
                outcome=ServiceSetupOutcome.UNSUPPORTED,
            )
        if status.state is ServiceLifecycleState.READY:
            progress(ServiceSetupProgress.READY)
            return self._resume(intent)

        if service.compatible:
            progress(ServiceSetupProgress.RESTARTING)
            restarted = self._daemon.restart(provider_ids)
            if self._closed.is_set():
                return self._failed(intent)
            if restarted.state is ServiceLifecycleState.READY:
                progress(ServiceSetupProgress.READY)
                return self._resume(intent)
        return self._install_or_block(
            intent=intent,
            interactive=interactive,
            decision=decision,
            progress=progress,
            provider_ids=provider_ids,
        )

    def _install_or_block(
        self,
        *,
        intent: DashboardIntent,
        interactive: bool,
        decision: ServiceSetupDecision,
        progress: ServiceSetupProgressSink,
        provider_ids: ProviderReadinessScope,
    ) -> ServiceSetupResult[DashboardIntent]:
        if not interactive:
            return ServiceSetupResult(
                intent=intent,
                outcome=ServiceSetupOutcome.NONINTERACTIVE,
            )
        if decision is ServiceSetupDecision.NOT_REQUESTED:
            return ServiceSetupResult(
                intent=intent,
                outcome=ServiceSetupOutcome.CONFIRMATION_REQUIRED,
            )
        if decision is ServiceSetupDecision.REFUSED:
            return ServiceSetupResult(
                intent=intent,
                outcome=ServiceSetupOutcome.REFUSED,
            )

        progress(ServiceSetupProgress.INSTALLING)
        installed = self._daemon.install(provider_ids)
        if self._closed.is_set():
            return self._failed(intent)
        if installed.state is ServiceLifecycleState.READY:
            progress(ServiceSetupProgress.READY)
            return self._resume(intent)
        return ServiceSetupResult(
            intent=intent,
            outcome=ServiceSetupOutcome.FAILED,
        )

    @staticmethod
    def _failed(
        intent: DashboardIntent,
    ) -> ServiceSetupResult[DashboardIntent]:
        return ServiceSetupResult(
            intent=intent,
            outcome=ServiceSetupOutcome.FAILED,
        )

    @staticmethod
    def _resume(
        intent: DashboardIntent,
    ) -> ServiceSetupResult[DashboardIntent]:
        return ServiceSetupResult(
            intent=intent,
            outcome=ServiceSetupOutcome.RESUME,
        )


def _provider_ids(intent: DashboardIntent) -> ProviderReadinessScope:
    """Return every provider whose capability the intent requires."""
    if isinstance(intent, ActivateOrRepairIntent | RefreshAccountIntent):
        return (intent.provider_id,)
    if isinstance(intent, RefreshDueAccountsIntent):
        return _ALL_PROVIDER_IDS
    assert_never(intent)
