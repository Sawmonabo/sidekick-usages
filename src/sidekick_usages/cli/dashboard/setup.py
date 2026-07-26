"""Guided per-user service readiness for pending dashboard actions."""

from threading import Event, Lock
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
    ServiceSetupResult,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.lifecycle.ports import (
    ServiceLifecycleObserver,
    discard_service_lifecycle_observation,
)
from sidekick_usages.daemon.models.lifecycle import DaemonOperationResult
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceFailureCode,
    ServiceLifecycleState,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.usage.dashboard.models import DashboardService

_ALL_PROVIDER_IDS = tuple(ProviderId)


class GuidedServiceSetup:
    """Prepare the resident service before one pending dashboard action."""

    def __init__(
        self,
        daemon: DaemonManager,
        acknowledgements: ServiceSetupAcknowledgementStore,
    ) -> None:
        self._daemon = daemon
        self._acknowledgements = acknowledgements
        self._closed = Event()
        self._state_lock = Lock()

    def close(self) -> None:
        """Interrupt only dashboard-owned lifecycle observation."""
        with self._state_lock:
            self._closed.set()
        self._daemon.cancel()

    def prepare(
        self,
        *,
        service: DashboardService,
        intent: DashboardIntent,
        interactive: bool,
        decision: ServiceSetupDecision,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> ServiceSetupResult[DashboardIntent]:
        """Return the original intent only after bounded service readiness."""
        provider_ids = _provider_ids(intent)
        status = self._daemon.status(provider_ids, progress=progress)
        completion = self._approved_completion(
            intent,
            self._completion(intent, status),
            decision,
        )
        if completion is not None:
            return completion
        if status.state is ServiceLifecycleState.FEATURE_DISABLED:
            return ServiceSetupResult(
                intent=intent,
                outcome=ServiceSetupOutcome.UNSUPPORTED,
            )

        if service.compatible:
            restarted = self._daemon.restart(
                provider_ids,
                progress=progress,
            )
            completion = self._approved_completion(
                intent,
                self._completion(intent, restarted),
                decision,
            )
            if completion is not None:
                return completion
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
        progress: ServiceLifecycleObserver,
        provider_ids: ProviderReadinessScope,
    ) -> ServiceSetupResult[DashboardIntent]:
        blocked_outcome = _blocked_setup_outcome(interactive, decision)
        if blocked_outcome is ServiceSetupOutcome.CONFIRMATION_REQUIRED:
            try:
                acknowledged = self._acknowledgements.matches(PROTOCOL_VERSION)
            except PersistenceError:
                return self._failed(intent)
            if acknowledged:
                blocked_outcome = None
        if blocked_outcome is not None:
            return ServiceSetupResult(
                intent=intent,
                outcome=blocked_outcome,
            )

        installed = self._daemon.install(
            provider_ids,
            progress=progress,
        )
        completion = self._approved_completion(
            intent,
            self._completion(intent, installed),
            decision,
        )
        if completion is None:
            return self._failed(intent)
        return completion

    def _approved_completion(
        self,
        intent: DashboardIntent,
        completion: ServiceSetupResult[DashboardIntent] | None,
        decision: ServiceSetupDecision,
    ) -> ServiceSetupResult[DashboardIntent] | None:
        if (
            completion is not None
            and completion.outcome is ServiceSetupOutcome.RESUME
            and decision is ServiceSetupDecision.APPROVED
        ):
            return self._acknowledge_success(intent)
        return completion

    def _acknowledge_success(
        self,
        intent: DashboardIntent,
    ) -> ServiceSetupResult[DashboardIntent]:
        with self._state_lock:
            if self._closed.is_set():
                return self._failed(intent)
            try:
                self._acknowledgements.acknowledge(PROTOCOL_VERSION)
            except PersistenceError:
                return self._failed(intent)
        return self._resume(intent)

    def _completion(
        self,
        intent: DashboardIntent,
        result: DaemonOperationResult,
    ) -> ServiceSetupResult[DashboardIntent] | None:
        """Classify a completed lifecycle operation."""
        if self._closed.is_set():
            return self._failed(intent)
        provider_failure = _provider_failure(intent, result)
        if provider_failure is not None:
            return provider_failure
        if result.state is not ServiceLifecycleState.READY:
            return None
        return self._resume(intent)

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


def _blocked_setup_outcome(
    interactive: bool,
    decision: ServiceSetupDecision,
) -> ServiceSetupOutcome | None:
    """Return why installation cannot proceed without changing the service."""
    if not interactive:
        return ServiceSetupOutcome.NONINTERACTIVE
    if decision is ServiceSetupDecision.NOT_REQUESTED:
        return ServiceSetupOutcome.CONFIRMATION_REQUIRED
    if decision is ServiceSetupDecision.REFUSED:
        return ServiceSetupOutcome.REFUSED
    return None


def _provider_failure(
    intent: DashboardIntent,
    result: DaemonOperationResult,
) -> ServiceSetupResult[DashboardIntent] | None:
    """Return exact provider guidance without mutating a healthy service."""
    if (
        result.failure_code
        is not ServiceFailureCode.PROVIDER_CAPABILITY_UNAVAILABLE
    ):
        return None
    provider_id = result.failure_provider_id
    if provider_id is None:
        raise AssertionError("Provider capability failure lost its provider.")
    return ServiceSetupResult(
        intent=intent,
        outcome=ServiceSetupOutcome.PROVIDER_UNAVAILABLE,
        provider_id=provider_id,
    )
