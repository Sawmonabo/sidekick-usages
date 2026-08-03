"""Typed boundaries for the isolated interactive dashboard."""

from collections.abc import Callable
from threading import Lock
from typing import Protocol

from sidekick_usages.cli.dashboard.models.controller import (
    DashboardIntent,
    DashboardMove,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardActionRequest,
    DashboardConfirmationKind,
    DashboardSessionView,
    DashboardStartupReconciliation,
)
from sidekick_usages.cli.dashboard.models.setup import ServiceSetupDecision
from sidekick_usages.core.selection.models import SelectionResult
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import SelectionStatus
from sidekick_usages.usage.dashboard.models import (
    DashboardService,
    DashboardSnapshot,
)


class DashboardSnapshotSource(Protocol):
    """Load one secret-free cached dashboard projection."""

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return cached state constrained to one optional provider."""
        ...


class DashboardLookupSink(Protocol):
    """Publish lookup outcomes into one interactive session."""

    def publish_lookup_snapshot(
        self,
        snapshot: DashboardSnapshot,
    ) -> bool:
        """Publish one resolved snapshot and report whether it was accepted."""
        ...

    def publish_lookup_failure(
        self,
        *,
        diagnostic_unavailable: bool = False,
    ) -> None:
        """Publish one terminal lookup failure."""
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

    def publish_selection_status(
        self,
        provider_id: ProviderId,
        status: SelectionStatus | SelectionResult | None,
    ) -> None:
        """Publish or clear one canonical provider selection snapshot."""
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
        result: SelectionResult | None,
    ) -> None:
        """Apply one successful correlated action result."""
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


class DashboardActionOwner(Protocol):
    """Own serialized supervisor actions after the first dashboard frame."""

    def close(self) -> None:
        """Stop observation without cancelling durable supervisor work."""
        ...

    def execute(self, request: DashboardActionRequest) -> None:
        """Execute one exact queued dashboard action."""
        ...

    def reconcile_startup(
        self,
        provider_ids: tuple[ProviderId, ...],
    ) -> None:
        """Reconcile displayed providers without blocking dashboard input."""
        ...


class DashboardLookupOwner(Protocol):
    """Own lookup execution and immutable cached-state overlays."""

    def start(self) -> None:
        """Start exactly one isolated lookup owner."""
        ...

    def close(self) -> None:
        """Cancel and join the lookup owner exactly once."""
        ...

    def apply(self, snapshot: DashboardSnapshot) -> DashboardSnapshot:
        """Apply completed lookup outcomes to cached state."""
        ...


class DashboardSessionRuntimeFactory(Protocol):
    """Build both dashboard runtime owners after the cached first frame."""

    def __call__(
        self,
        *,
        action_sink: DashboardActionSink,
        lookup_sink: DashboardLookupSink,
        snapshot_lock: Lock,
    ) -> tuple[DashboardActionOwner, DashboardLookupOwner]:
        """Return both owners bound to one session."""
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
        """Restore verified focus unless selection is in flight."""
        ...

    def select_account(self) -> None:
        """Queue selection or publish one typed refusal."""
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
