"""Atomic worker-result persistence keyed by operation ID."""

from pathlib import Path

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.worker import (
    decode_worker_result,
    encode_worker_result,
)
from sidekick_usages.persistence.state.files import (
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

__all__ = ["WorkerResultStore"]

_RESULT_DIRECTORY = "results"


class WorkerResultStore:
    """Persist one sanitized result per isolated operation."""

    def __init__(self, operations_root: Path) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        self._root = operations_root / _RESULT_DIRECTORY

    def load(self, operation_id: OperationId) -> WorkerResult | None:
        """Load one exact result when present."""
        filesystem = self._filesystem(operation_id)
        snapshot = filesystem.read_opaque_private()
        if snapshot is None:
            return None
        result = decode_worker_result(snapshot.data)
        if result.operation_id != operation_id:
            raise ValueError("Worker result operation binding changed.")
        return result

    def save(self, result: WorkerResult) -> None:
        """Atomically save one result without replacing a different value."""
        filesystem = self._filesystem(result.operation_id)
        lock = PersistenceLock(filesystem)
        payload = encode_worker_result(result)
        with lock.hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot = filesystem.read_opaque_private()
            if snapshot is not None:
                if snapshot.data == payload:
                    return
                raise ValueError("Worker result already exists.")
            filesystem.commit_opaque_private(
                payload,
                expected_source=AuthorityExpectation.ABSENT,
            )

    def delete(self, operation_id: OperationId) -> bool:
        """Delete one exact consumed result under its qualified lock."""
        filesystem = self._filesystem(operation_id)
        lock = PersistenceLock(filesystem)
        with lock.hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot = filesystem.read_opaque_private()
            if snapshot is None:
                return False
            filesystem.delete_opaque_private(snapshot.fingerprint)
            return True

    def _filesystem(
        self,
        operation_id: OperationId,
    ) -> ManagedStateFilesystem:
        return ManagedStateFilesystem(
            self._root / f"{operation_id}.json",
            decode_worker_result,
        )
