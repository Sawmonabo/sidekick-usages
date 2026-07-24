"""Durable supervisor scheduling and callback priority."""

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationPriority,
    OperationState,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import WorkerExit, WorkerResult
from sidekick_usages.daemon.types.ports import OperationEventSink
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.pool import (
    WorkerLaunchError,
    WorkerPool,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore

_SCHEDULE_INTERVAL = timedelta(minutes=5)
_MINIMUM_RETRY = timedelta(minutes=1)
_MAXIMUM_RETRY = timedelta(hours=1)


class NullOperationEventSink:
    """No-op event sink for noninteractive service operation."""

    def started(self, operation: DueOperation) -> None:
        del operation

    def completed(self, completion: SchedulerCompletion) -> None:
        del completion

    def failed(self, operation: DueOperation, code: str) -> None:
        del operation, code


class DurableScheduler:
    """Coordinate durable queue state around isolated worker lifetimes."""

    def __init__(
        self,
        queue: OperationQueueStore,
        results: WorkerResultStore,
        workers: WorkerPool,
        clock: Clock,
        *,
        events: OperationEventSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._results = results
        self._workers = workers
        self._clock = clock
        self._events = events or NullOperationEventSink()
        self._monotonic = monotonic

    @property
    def active_count(self) -> int:
        """Return the current bounded worker count."""
        return self._workers.active_count

    def recover(self) -> tuple[SchedulerCompletion, ...]:
        """Recover committed results before retrying interrupted workers."""
        self._queue.recover()
        recovered: list[SchedulerCompletion] = []
        now = self._clock.now()
        for operation in self._queue.load():
            if operation.state is not OperationState.RUNNING:
                continue
            result = self._safe_result(operation)
            if result is None:
                result = WorkerResult(
                    operation_id=operation.operation_id,
                    outcome=WorkerOutcome.TRANSIENT_FAILURE,
                    finished_at=now,
                    failure_code="worker_interrupted",
                )
            completion = self._apply_result(operation, result, now)
            recovered.append(completion)
        return tuple(recovered)

    def dispatch_due(self) -> tuple[DueOperation, ...]:
        """Start every due operation whose independent lane is available."""
        started: list[DueOperation] = []
        now = self._clock.now()
        monotonic_now = self._monotonic()
        for operation in self._queue.due(now):
            if (
                operation.priority is OperationPriority.CODEX_CALLBACK
                and not self._prepare_callback(operation, now)
            ):
                continue
            if not self._workers.can_start(operation):
                continue
            running = self._queue.transition(
                operation.operation_id,
                OperationState.RUNNING,
                updated_at=now,
            )
            try:
                self._workers.start(
                    running,
                    monotonic_now=monotonic_now,
                )
            except WorkerLaunchError:
                self._queue.transition(
                    running.operation_id,
                    OperationState.RETRY_WAIT,
                    updated_at=now,
                    due_at=now + _retry_delay(running.attempts),
                    failure_code="worker_launch_failed",
                )
                self._events.failed(running, "worker_launch_failed")
                continue
            self._events.started(running)
            started.append(running)
        return tuple(started)

    def collect(self) -> tuple[SchedulerCompletion, ...]:
        """Reap completions and hard timeouts without coupling accounts."""
        exits = (
            *self._workers.reap_completed(),
            *self._workers.expire(self._monotonic()),
        )
        completed: list[SchedulerCompletion] = []
        for worker_exit in exits:
            try:
                completion = self._complete_exit(worker_exit)
            except Exception:
                self._events.failed(
                    worker_exit.operation,
                    "scheduler_result_failed",
                )
                continue
            completed.append(completion)
            self._events.completed(completion)
        return tuple(completed)

    def next_wait_seconds(self) -> float | None:
        """Return the next durable or monotonic deadline for ``select``."""
        now = self._clock.now()
        future_due = tuple(
            operation.due_at
            for operation in self._queue.load()
            if operation.state
            in {OperationState.SCHEDULED, OperationState.RETRY_WAIT}
            and operation.due_at > now
        )
        waits: list[float] = [
            max(0.0, (due_at - now).total_seconds()) for due_at in future_due
        ]
        worker_deadline = self._workers.next_deadline()
        if worker_deadline is not None:
            waits.append(max(0.0, worker_deadline - self._monotonic()))
        return min(waits) if waits else None

    def shutdown(self) -> tuple[SchedulerCompletion, ...]:
        """Terminate workers and durably reschedule their operations."""
        completed: list[SchedulerCompletion] = []
        for worker_exit in self._workers.shutdown():
            completion = self._complete_exit(worker_exit)
            completed.append(completion)
            self._events.completed(completion)
        return tuple(completed)

    def _prepare_callback(
        self,
        callback: DueOperation,
        now: datetime,
    ) -> bool:
        try:
            preempted = self._workers.preempt_for_callback(callback)
        except WorkerLaunchError:
            self._events.failed(callback, "callback_authority_busy")
            return False
        if preempted is None:
            return True
        result = WorkerResult(
            operation_id=preempted.operation.operation_id,
            outcome=WorkerOutcome.CANCELLED,
            finished_at=now,
            failure_code="worker_preempted",
        )
        completion = self._apply_result(preempted.operation, result, now)
        self._events.completed(completion)
        return True

    def _complete_exit(
        self,
        worker_exit: WorkerExit,
    ) -> SchedulerCompletion:
        operation = worker_exit.operation
        now = self._clock.now()
        if worker_exit.timed_out:
            result = WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.TIMED_OUT,
                finished_at=now,
                failure_code="worker_timed_out",
            )
        elif worker_exit.preempted:
            result = WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.CANCELLED,
                finished_at=now,
                failure_code="worker_preempted",
            )
        else:
            result = self._safe_result(operation)
            if worker_exit.exit_code != 0 or result is None:
                result = WorkerResult(
                    operation_id=operation.operation_id,
                    outcome=WorkerOutcome.TRANSIENT_FAILURE,
                    finished_at=now,
                    failure_code="worker_result_missing",
                )
        return self._apply_result(operation, result, now)

    def _safe_result(
        self,
        operation: DueOperation,
    ) -> WorkerResult | None:
        try:
            result = self._results.load(operation.operation_id)
        except Exception:
            return None
        if (
            result is not None
            and result.operation_id != operation.operation_id
        ):
            return None
        return result

    def _apply_result(
        self,
        operation: DueOperation,
        result: WorkerResult,
        now: datetime,
    ) -> SchedulerCompletion:
        if result.outcome is WorkerOutcome.SUCCEEDED:
            if operation.priority is OperationPriority.SCHEDULED:
                updated = self._queue.transition(
                    operation.operation_id,
                    OperationState.SCHEDULED,
                    updated_at=now,
                    due_at=now + _SCHEDULE_INTERVAL,
                )
                state: OperationState | None = updated.state
            else:
                self._queue.remove(
                    operation.operation_id,
                    expected_state=OperationState.RUNNING,
                )
                state = None
        elif result.outcome in {
            WorkerOutcome.ACTION_REQUIRED,
            WorkerOutcome.UNSUPPORTED,
        }:
            updated = self._queue.transition(
                operation.operation_id,
                OperationState.ACTION_REQUIRED,
                updated_at=now,
                failure_code=result.failure_code,
            )
            state = updated.state
        else:
            updated = self._queue.transition(
                operation.operation_id,
                OperationState.RETRY_WAIT,
                updated_at=now,
                due_at=now + _retry_delay(operation.attempts),
                failure_code=result.failure_code,
            )
            state = updated.state
        self._results.delete(operation.operation_id)
        return SchedulerCompletion(
            operation_id=operation.operation_id,
            state=state,
            outcome=result.outcome,
            failure_code=result.failure_code,
        )


def _retry_delay(attempts: int) -> timedelta:
    exponent = max(0, min(attempts - 1, 6))
    seconds = _MINIMUM_RETRY.total_seconds() * (2**exponent)
    return min(timedelta(seconds=seconds), _MAXIMUM_RETRY)
