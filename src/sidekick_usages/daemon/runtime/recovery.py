"""Supervisor recovery for unfinished provider activations."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation, SelectionResult
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

    def recover_all(self) -> tuple[SelectionResult, ...]:
        """Restore every gate and finalize only immediately proven work."""

    def reconciled(self) -> bool:
        """Return whether no selection journal remains active."""


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

    def recover_selection(self) -> tuple[SelectionResult, ...]:
        """Restore global selection gates without awaiting participants."""
        if self._selection_recovery is None:
            return ()
        return self._selection_recovery.recover_all()

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
