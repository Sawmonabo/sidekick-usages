"""Guided per-user service readiness for pending dashboard actions."""

from collections.abc import Callable

from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
    ServiceSetupProgress,
    ServiceSetupResult,
)
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.usage.dashboard.models import DashboardService

type ServiceSetupProgressSink = Callable[[ServiceSetupProgress], None]


def _discard_progress(_progress: ServiceSetupProgress) -> None:
    """Discard optional setup progress."""


class GuidedServiceSetup:
    """Prepare the resident service before one pending dashboard action."""

    def __init__(self, daemon: DaemonManager) -> None:
        self._daemon = daemon

    def prepare[IntentT](
        self,
        *,
        service: DashboardService,
        intent: IntentT,
        interactive: bool,
        decision: ServiceSetupDecision,
        progress: ServiceSetupProgressSink = _discard_progress,
    ) -> ServiceSetupResult[IntentT]:
        """Return the original intent only after bounded service readiness."""
        progress(ServiceSetupProgress.CHECKING)
        status = self._daemon.status()
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
            restarted = self._daemon.restart()
            if restarted.state is ServiceLifecycleState.READY:
                progress(ServiceSetupProgress.READY)
                return self._resume(intent)
        return self._install_or_block(
            intent=intent,
            interactive=interactive,
            decision=decision,
            progress=progress,
        )

    def _install_or_block[IntentT](
        self,
        *,
        intent: IntentT,
        interactive: bool,
        decision: ServiceSetupDecision,
        progress: ServiceSetupProgressSink,
    ) -> ServiceSetupResult[IntentT]:
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
        installed = self._daemon.install()
        if installed.state is ServiceLifecycleState.READY:
            progress(ServiceSetupProgress.READY)
            return self._resume(intent)
        return ServiceSetupResult(
            intent=intent,
            outcome=ServiceSetupOutcome.FAILED,
        )

    @staticmethod
    def _resume[IntentT](intent: IntentT) -> ServiceSetupResult[IntentT]:
        return ServiceSetupResult(
            intent=intent,
            outcome=ServiceSetupOutcome.RESUME,
        )
