"""Durable coalescing supervisor operation queue."""

from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.policy import (
    coalesce_due_operation,
    transition_operation,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.models.selection import (
    OperationQueueDocument,
    operation_queue_slot,
)
from sidekick_usages.persistence.schema.worker import (
    decode_operation_queue,
    encode_operation_queue,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

_QUEUE_BASENAME = "queue.json"


class OperationQueueStore:
    """Persist one coalescing operation slot per stable account and kind."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        self.root = root
        self.path = root / _QUEUE_BASENAME
        self._filesystem = ManagedStateFilesystem(
            self.path,
            decode_operation_queue,
        )
        self._lock = PersistenceLock(self._filesystem)

    def load(self) -> tuple[DueOperation, ...]:
        """Load every durable operation in deterministic slot order."""
        with self._lock.hold():
            return self._load_document().operations

    def observe(self) -> tuple[DueOperation, ...]:
        """Passively read durable operations without lock-sidecar writes."""
        return self._load_document().operations

    def get(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId | None,
        kind: OperationKind,
    ) -> DueOperation | None:
        """Load one exact provider and operation-owner slot."""
        return next(
            (
                operation
                for operation in self.load()
                if operation.provider_id is provider_id
                and operation.account_id == account_id
                and operation.kind is kind
            ),
            None,
        )

    def find(self, operation_id: OperationId) -> DueOperation | None:
        """Load one exact durable operation by correlation ID."""
        return next(
            (
                operation
                for operation in self.load()
                if operation.operation_id == operation_id
            ),
            None,
        )

    def enqueue(self, operation: DueOperation) -> DueOperation:
        """Create or coalesce one due event without growing the queue."""
        if operation.state is not OperationState.SCHEDULED:
            raise ValueError("Only scheduled work can be enqueued.")
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            slots = {
                operation_queue_slot(current): current
                for current in document.operations
            }
            key = operation_queue_slot(operation)
            current = slots.get(key)
            effective = (
                operation
                if current is None
                else coalesce_due_operation(current, operation)
            )
            slots[key] = effective
            self._commit(tuple(slots.values()), snapshot)
            return effective

    def transition(
        self,
        operation_id: OperationId,
        state: OperationState,
        *,
        updated_at: datetime,
        due_at: datetime | None = None,
        failure_code: str | None = None,
        priority: OperationPriority | None = None,
    ) -> DueOperation:
        """Advance one exact durable operation through a legal state edge."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            current = next(
                (
                    operation
                    for operation in document.operations
                    if operation.operation_id == operation_id
                ),
                None,
            )
            if current is None:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            effective = transition_operation(
                current,
                state,
                updated_at=updated_at,
                due_at=due_at,
                failure_code=failure_code,
                priority=priority,
            )
            operations = tuple(
                effective
                if operation.operation_id == operation_id
                else operation
                for operation in document.operations
            )
            self._commit(operations, snapshot)
            return effective

    def due(self, now: datetime) -> tuple[DueOperation, ...]:
        """Return due work ordered by lane, wall time, account, and kind."""
        current_time = as_utc(now)
        due = (
            operation
            for operation in self.load()
            if operation.state
            in {OperationState.SCHEDULED, OperationState.RETRY_WAIT}
            and operation.due_at <= current_time
        )
        return tuple(
            sorted(
                due,
                key=lambda operation: (
                    operation.priority.rank,
                    operation.due_at,
                    operation.provider_id.value,
                    str(operation.account_id),
                    operation.kind.value,
                ),
            )
        )

    def recover_running(
        self,
        updated_at: datetime,
    ) -> tuple[DueOperation, ...]:
        """Move interrupted running work into immediately due retry state."""
        current_time = as_utc(updated_at)
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            recovered = tuple(
                transition_operation(
                    operation,
                    OperationState.RETRY_WAIT,
                    updated_at=current_time,
                    due_at=current_time,
                    failure_code="worker_interrupted",
                )
                if operation.state is OperationState.RUNNING
                else operation
                for operation in document.operations
            )
            self._commit(recovered, snapshot)
            return recovered

    def discard_orphan_callbacks(self) -> tuple[DueOperation, ...]:
        """Remove callbacks whose in-memory response owner is gone."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            discarded = tuple(
                operation
                for operation in document.operations
                if operation.kind is OperationKind.CODEX_CALLBACK
            )
            if not discarded:
                return ()
            retained = tuple(
                operation
                for operation in document.operations
                if operation not in discarded
            )
            self._commit(retained, snapshot)
            return discarded

    def discard_stale_activations(
        self,
        provider_id: ProviderId,
        active_account_id: SidekickAccountId | None,
        verified_before: datetime,
    ) -> tuple[DueOperation, ...]:
        """Remove older pending switches superseded by native proof."""
        cutoff = as_utc(verified_before)
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            stale = tuple(
                operation
                for operation in document.operations
                if operation.provider_id is provider_id
                and operation.kind is OperationKind.ACTIVATE
                and operation.account_id != active_account_id
                and operation.state is not OperationState.RUNNING
                and operation.updated_at < cutoff
            )
            if not stale:
                return ()
            stale_ids = frozenset(
                operation.operation_id for operation in stale
            )
            retained = tuple(
                operation
                for operation in document.operations
                if operation.operation_id not in stale_ids
            )
            self._commit(retained, snapshot)
            return stale

    def remove_account(self, account_id: SidekickAccountId) -> int:
        """Remove idle due state or reject a running account operation."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            owned = tuple(
                operation
                for operation in document.operations
                if operation.account_id == account_id
            )
            if any(
                operation.state is OperationState.RUNNING
                for operation in owned
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.RUNNING_OPERATION
                )
            if not owned:
                return 0
            retained = tuple(
                operation
                for operation in document.operations
                if operation.account_id != account_id
            )
            self._commit(retained, snapshot)
            return len(owned)

    def remove(
        self,
        operation_id: OperationId,
        *,
        expected_state: OperationState,
    ) -> DueOperation:
        """Remove one exact operation only from its proven state."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            current = next(
                (
                    operation
                    for operation in document.operations
                    if operation.operation_id == operation_id
                ),
                None,
            )
            if current is None or current.state is not expected_state:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            retained = tuple(
                operation
                for operation in document.operations
                if operation.operation_id != operation_id
            )
            self._commit(retained, snapshot)
            return current

    def recover(self) -> None:
        """Discard bounded interrupted write candidates."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            self._load_document()

    def _load_document(self) -> OperationQueueDocument:
        return self._document(self._filesystem.read_opaque_private())

    @staticmethod
    def _document(
        snapshot: FileSnapshot | None,
    ) -> OperationQueueDocument:
        if snapshot is None:
            return OperationQueueDocument()
        return decode_operation_queue(snapshot.data)

    def _commit(
        self,
        operations: tuple[DueOperation, ...],
        snapshot: FileSnapshot | None,
    ) -> None:
        document = OperationQueueDocument(operations)
        payload = encode_operation_queue(document)
        if snapshot is not None and snapshot.data == payload:
            return
        self._filesystem.commit_opaque_private(
            payload,
            expected_source=(
                AuthorityExpectation.ABSENT
                if snapshot is None
                else snapshot.fingerprint
            ),
        )
