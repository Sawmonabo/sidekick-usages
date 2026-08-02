"""Qualified execution inside an isolated worker process."""

from collections.abc import Callable

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import (
    DueOperation,
    RelatedRuntimeAuthority,
)
from sidekick_usages.core.selection.types import OperationState
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.ports import (
    ProviderWorkerExecutor,
    WorkerExecutor,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    OperationAuthorityLock,
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore


class UnsupportedWorkerExecutor:
    """Truthful foundation executor until provider adapters are installed."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: OperationAuthority,
    ) -> WorkerResult:
        """Return a typed unsupported result without opening credentials."""
        authority.require(operation.required_account_id)
        return WorkerResult(
            operation_id=operation.operation_id,
            outcome=WorkerOutcome.UNSUPPORTED,
            finished_at=self._clock.now(),
            failure_code="provider_unsupported",
        )


def managed_worker_result(
    operation: DueOperation,
    clock: Clock,
    *,
    succeeded: bool,
    action_required: bool,
    timed_out: bool,
    failure_code: str,
) -> WorkerResult:
    """Map one provider-managed outcome to a sanitized worker result."""
    if succeeded:
        return worker_success(operation, clock)
    outcome = (
        WorkerOutcome.ACTION_REQUIRED
        if action_required
        else (
            WorkerOutcome.TIMED_OUT
            if timed_out
            else WorkerOutcome.TRANSIENT_FAILURE
        )
    )
    return worker_failure(operation, outcome, failure_code, clock)


def worker_success(
    operation: DueOperation,
    clock: Clock,
    related_runtime_authority: RelatedRuntimeAuthority | None = None,
) -> WorkerResult:
    """Return one sanitized successful worker result."""
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=WorkerOutcome.SUCCEEDED,
        finished_at=clock.now(),
        related_runtime_authority=related_runtime_authority,
    )


def worker_no_change(
    operation: DueOperation,
    clock: Clock,
    related_runtime_authority: RelatedRuntimeAuthority | None = None,
) -> WorkerResult:
    """Return one successful worker result with unchanged authority."""
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=WorkerOutcome.NO_CHANGE,
        finished_at=clock.now(),
        related_runtime_authority=related_runtime_authority,
    )


def worker_failure(
    operation: DueOperation,
    outcome: WorkerOutcome,
    failure_code: str,
    clock: Clock,
) -> WorkerResult:
    """Return one sanitized failed worker result."""
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=outcome,
        finished_at=clock.now(),
        failure_code=failure_code,
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
    with authority_lock.hold() as authority:
        return _execute_worker(
            operation,
            results,
            lambda: executor.execute(operation, authority),
            clock,
        )


def run_provider_worker(
    operation_id: OperationId,
    queue: OperationQueueStore,
    results: WorkerResultStore,
    authority_lock: ProviderMutationLock,
    executor: ProviderWorkerExecutor,
    clock: Clock,
) -> bool:
    """Execute one provider-mutating operation under provider-first locks."""
    operation = queue.find(operation_id)
    if operation is None or operation.state is not OperationState.RUNNING:
        return False
    with authority_lock.hold() as provider_authority:
        provider_authority.require(operation.provider_id)
        return _execute_worker(
            operation,
            results,
            lambda: executor.execute(operation, provider_authority),
            clock,
        )


def _execute_worker(
    operation: DueOperation,
    results: WorkerResultStore,
    execute: Callable[[], WorkerResult],
    clock: Clock,
) -> bool:
    try:
        result = execute()
    except Exception:
        result = WorkerResult(
            operation_id=operation.operation_id,
            outcome=WorkerOutcome.TRANSIENT_FAILURE,
            finished_at=clock.now(),
            failure_code="worker_failed",
        )
    if result.operation_id != operation.operation_id:
        result = WorkerResult(
            operation_id=operation.operation_id,
            outcome=WorkerOutcome.TRANSIENT_FAILURE,
            finished_at=clock.now(),
            failure_code="worker_result_mismatch",
        )
    results.save(result)
    return True
