"""Supervisor recovery for unfinished provider activations."""

from collections.abc import Callable
from datetime import datetime

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore


class ActivationRecoveryScheduler:
    """Enroll unfinished journals before switching can become ready."""

    def __init__(
        self,
        journals: ActivationJournalStore,
        queue: OperationQueueStore,
        *,
        operation_id_factory: Callable[[], OperationId] = new_operation_id,
    ) -> None:
        self._journals = journals
        self._queue = queue
        self._operation_id_factory = operation_id_factory

    def enroll(self, now: datetime) -> tuple[DueOperation, ...]:
        """Persist one immediate reconcile slot per unfinished provider."""
        enrolled: list[DueOperation] = []
        for provider_id in ProviderId:
            active = self._journals.load(provider_id).active
            if active is None:
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
        return all(
            self._journals.load(provider_id).active is None
            for provider_id in ProviderId
        )
