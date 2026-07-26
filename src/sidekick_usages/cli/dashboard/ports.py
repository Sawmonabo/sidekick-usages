"""Typed boundaries for the isolated interactive dashboard."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

from sidekick_usages.cli.dashboard.models.controller import (
    DashboardIntent,
    DashboardMove,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
    DashboardSessionView,
    DashboardStartupReconciliation,
)
from sidekick_usages.cli.dashboard.models.setup import ServiceSetupDecision
from sidekick_usages.cli.dashboard.models.use import UseActivationResult
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import (
    ControlActionTerminalPayload,
    ControlEvent,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardService,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventObserver,
    UsageLookupWorkerResult,
)


class AccountActivation(Protocol):
    """Activate one exact saved account through the local supervisor."""

    def __call__(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        allow_remote_control_disconnect: bool,
    ) -> UseActivationResult:
        """Return the supervisor's sanitized activation outcome."""
        ...


class DashboardLookupWorker(Protocol):
    """Run and cancel one isolated global account-lookup process."""

    def run(
        self,
        observe: UsageLookupEventObserver | None = None,
    ) -> UsageLookupWorkerResult:
        """Stream stable account completions and return terminal state."""
        ...

    def cancel(self) -> None:
        """Request bounded process-group termination and reaping."""
        ...


class DashboardControlClient(Protocol):
    """Observe one local supervisor connection."""

    def snapshot(self) -> Iterator[ControlEvent]:
        """Return one current sanitized service snapshot."""
        ...

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        *,
        allow_remote_control_disconnect: bool = False,
    ) -> Iterator[ControlEvent]:
        """Activate one stable account."""
        ...

    def refresh_account(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Iterator[ControlEvent]:
        """Refresh one stable account without selecting it."""
        ...

    def refresh_all(self) -> Iterator[ControlEvent]:
        """Schedule every due account for maintenance."""
        ...

    def reconcile(
        self,
        provider_id: ProviderId,
    ) -> Iterator[ControlEvent]:
        """Reconcile one provider's current native account."""
        ...

    def close(self) -> None:
        """Stop observing without cancelling durable provider work."""
        ...


class DashboardControlConnector(Protocol):
    """Open one local supervisor connection."""

    def __call__(self, socket_path: Path) -> DashboardControlClient:
        """Connect to the exact same-user control socket."""
        ...


class DashboardActionSink(Protocol):
    """Publish serialized action state into one atomic dashboard view."""

    @property
    def service(self) -> DashboardService:
        """Return the latest cached service hint."""
        ...

    @property
    def stopping(self) -> bool:
        """Return whether the dashboard is closing."""
        ...

    def publish_progress(self, message: str) -> None:
        """Publish one sanitized action phase."""
        ...

    def request_confirmation(
        self,
        kind: DashboardConfirmationKind,
        message: str,
    ) -> ServiceSetupDecision:
        """Wait for one contextual user decision."""
        ...

    def action_completed(
        self,
        intent: DashboardIntent,
        terminal: ControlActionTerminalPayload,
    ) -> None:
        """Apply one correlated terminal action result."""
        ...

    def action_failed(self, intent: DashboardIntent) -> None:
        """Restore provider-proven state after one action failure."""
        ...

    def action_error(
        self,
        intent: DashboardIntent,
        message: str,
    ) -> None:
        """Restore cached truth and show one fixed corrective action."""
        ...

    def startup_reconciled(
        self,
        result: DashboardStartupReconciliation,
    ) -> None:
        """Publish one provider's passive startup read-back result."""
        ...


class DashboardSessionPort(Protocol):
    """Own one atomic interactive view and its two background workers."""

    @property
    def view(self) -> DashboardSessionView:
        """Return one internally consistent immutable dashboard view."""
        ...

    def bind_invalidator(self, invalidate: Callable[[], None]) -> None:
        """Bind the prompt-toolkit thread-safe redraw callback."""
        ...

    def start(self) -> None:
        """Start exactly one lookup and one action owner."""
        ...

    def close(self) -> None:
        """Stop observation, cancel lookup work, and join both owners."""
        ...

    def move(self, direction: DashboardMove) -> None:
        """Move the preview cursor."""
        ...

    def focus_next_provider(self) -> None:
        """Focus the next displayed provider."""
        ...

    def restore(self) -> None:
        """Restore verified focus unless activation is in flight."""
        ...

    def activate(self) -> None:
        """Queue one account activation without blocking input."""
        ...

    def refresh_account(self) -> None:
        """Queue one account refresh without blocking input."""
        ...

    def refresh_due_accounts(self) -> None:
        """Queue one global maintenance request without blocking input."""
        ...

    def toggle_help(self) -> None:
        """Toggle bounded keyboard guidance."""
        ...

    def confirm(self, approved: bool) -> None:
        """Resolve the currently displayed typed confirmation."""
        ...
