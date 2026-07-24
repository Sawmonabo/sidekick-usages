"""Qualified execution inside an isolated worker process."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import OperationState
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.ports import WorkerExecutor
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore


class UnsupportedWorkerExecutor:
    """Truthful foundation executor until provider adapters are installed."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def execute(self, operation: DueOperation) -> WorkerResult:
        """Return a typed unsupported result without opening credentials."""
        return WorkerResult(
            operation_id=operation.operation_id,
            outcome=WorkerOutcome.UNSUPPORTED,
            finished_at=self._clock.now(),
            failure_code="provider_unsupported",
        )


def run_isolated_worker(
    operation_id: OperationId,
    queue: OperationQueueStore,
    results: WorkerResultStore,
    authority_lock: OperationAuthorityLock,
    executor: WorkerExecutor,
    clock: Clock,
) -> bool:
    """Execute one running operation and atomically persist a safe result."""
    operation = queue.find(operation_id)
    if operation is None or operation.state is not OperationState.RUNNING:
        return False
    with authority_lock.hold():
        try:
            result = executor.execute(operation)
        except Exception:
            result = WorkerResult(
                operation_id=operation_id,
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                finished_at=clock.now(),
                failure_code="worker_failed",
            )
        if result.operation_id != operation_id:
            result = WorkerResult(
                operation_id=operation_id,
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                finished_at=clock.now(),
                failure_code="worker_result_mismatch",
            )
        results.save(result)
    return True
