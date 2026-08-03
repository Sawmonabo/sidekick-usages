"""Supervisor recovery for unfinished provider activations."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore


class GlobalSelectionRecovery(Protocol):
    """Nonblocking provider-neutral selection-journal recovery."""

    def restore_all(self) -> tuple[ProviderId, ...]:
        """Restore active gates without provider work."""

    def enqueue_restored_readbacks(self) -> tuple[DueOperation, ...]:
        """Enqueue restored provider readbacks without provider I/O."""

    def resume(self, provider_id: ProviderId) -> None:
        """Resume one restored provider after runtime requalification."""

    def reconciled(self) -> bool:
        """Return whether no selection journal remains active."""

    def close(self) -> None:
        """Release live selection waiters without cancelling work."""


class ActivationRecoveryScheduler:
    """Enroll unfinished journals before switching can become ready."""

    def __init__(
        self,
        journals: ActivationJournalStore,
        queue: OperationQueueStore,
        *,
        operation_id_factory: Callable[[], OperationId] = new_operation_id,
        selection_recovery: GlobalSelectionRecovery | None = None,
    ) -> None:
        self._journals = journals
        self._queue = queue
        self._operation_id_factory = operation_id_factory
        self._selection_recovery = selection_recovery

    def restore_selection(self) -> tuple[ProviderId, ...]:
        """Restore global selection gates without provider readback."""
        if self._selection_recovery is None:
            return ()
        return self._selection_recovery.restore_all()

    def enqueue_selection_readbacks(self) -> tuple[DueOperation, ...]:
        """Enqueue restored selection readbacks after socket acceptance."""
        if self._selection_recovery is None:
            return ()
        return self._selection_recovery.enqueue_restored_readbacks()

    def close_selection(self) -> None:
        """Release selection waiters during supervisor shutdown."""
        if self._selection_recovery is not None:
            self._selection_recovery.close()

    def resume_selection(self, provider_id: ProviderId) -> None:
        """Resume recovery after one resident provider becomes available."""
        if self._selection_recovery is not None:
            self._selection_recovery.resume(provider_id)

    def enroll(self, now: datetime) -> tuple[DueOperation, ...]:
        """Match durable recovery slots exactly to unfinished journals."""
        active_by_provider = {
            provider_id: self._journals.load(provider_id).active
            for provider_id in ProviderId
        }
        for operation in self._queue.load():
            if operation.kind is not OperationKind.RECONCILE:
                continue
            active = active_by_provider[operation.provider_id]
            if (
                active is not None
                and active.target_account_id == operation.account_id
            ):
                continue
            if operation.state is OperationState.RUNNING:
                continue
            try:
                self._queue.remove(
                    operation.operation_id,
                    expected_state=operation.state,
                )
            except ManagedStateConflictError:
                continue
        enrolled: list[DueOperation] = []
        for provider_id, active in active_by_provider.items():
            if active is None:
                continue
            existing = self._queue.get(
                provider_id,
                active.target_account_id,
                OperationKind.RECONCILE,
            )
            if existing is not None:
                enrolled.append(existing)
                continue
            operation = DueOperation(
                operation_id=self._operation_id_factory(),
                provider_id=provider_id,
                account_id=active.target_account_id,
                kind=OperationKind.RECONCILE,
                priority=OperationPriority.INTERACTIVE,
                state=OperationState.SCHEDULED,
                due_at=now,
                updated_at=now,
            )
            enrolled.append(self._queue.enqueue(operation))
        return tuple(enrolled)

    def reconciled(self) -> bool:
        """Return whether every provider journal has no active transaction."""
        activations_reconciled = all(
            self._journals.load(provider_id).active is None
            for provider_id in ProviderId
        )
        return activations_reconciled and (
            self._selection_recovery is None
            or self._selection_recovery.reconciled()
        )
