"""Durable supervisor scheduling and callback priority."""

import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import WorkerExit, WorkerResult
from sidekick_usages.daemon.types.ports import (
    OperationEventSink,
    OperationExchangePreparer,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.exchange import (
    operation_requires_worker_exchange,
)
from sidekick_usages.daemon.worker.pool import (
    WorkerLaunchError,
    WorkerPool,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
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
        exchange_preparer: OperationExchangePreparer | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._results = results
        self._workers = workers
        self._clock = clock
        self._events = events or NullOperationEventSink()
        self._exchange_preparer = exchange_preparer
        self._monotonic = monotonic

    @property
    def active_count(self) -> int:
        """Return the current bounded worker count."""
        return self._workers.active_count

    def recover(self) -> tuple[SchedulerCompletion, ...]:
        """Recover committed results before retrying interrupted workers."""
        self._queue.recover()
        for callback in self._queue.discard_callbacks():
            self._results.delete(callback.operation_id)
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
            callback = operation.priority is OperationPriority.CODEX_CALLBACK
            requires_exchange_preparation = (
                operation_requires_worker_exchange(operation) and not callback
            )
            if (
                requires_exchange_preparation
                and not self._workers.has_capacity_for(operation)
            ):
                continue
            if requires_exchange_preparation and (
                self._exchange_preparer is None
                or not self._exchange_preparer.prepare_operation(operation)
            ):
                continue
            if callback and not self._prepare_callback(
                operation,
                monotonic_now,
            ):
                continue
            if not self._workers.can_start(operation):
                if requires_exchange_preparation:
                    self._workers.cancel_exchange(operation.operation_id)
                continue
            try:
                running = self._queue.transition(
                    operation.operation_id,
                    OperationState.RUNNING,
                    updated_at=now,
                )
            except ManagedStateConflictError:
                self._workers.cancel_exchange(operation.operation_id)
                continue
            try:
                self._workers.start(
                    running,
                    monotonic_now=monotonic_now,
                )
            except WorkerLaunchError:
                if running.kind is OperationKind.CODEX_CALLBACK:
                    with suppress(ManagedStateConflictError):
                        self._queue.remove(
                            running.operation_id,
                            expected_state=OperationState.RUNNING,
                        )
                else:
                    self._queue.transition(
                        running.operation_id,
                        OperationState.RETRY_WAIT,
                        updated_at=now,
                        due_at=now + _retry_delay(running.attempts),
                        failure_code="worker_launch_failed",
                    )
                self._workers.cancel_exchange(running.operation_id)
                self._events.failed(running, "worker_launch_failed")
                continue
            self._events.started(running)
            started.append(running)
        return tuple(started)

    def collect(self) -> tuple[SchedulerCompletion, ...]:
        """Reap completions and hard timeouts without coupling accounts."""
        monotonic_now = self._monotonic()
        exits = (
            *self._workers.reap_completed(monotonic_now),
            *self._workers.expire(monotonic_now),
        )
        completed: list[SchedulerCompletion] = []
        for worker_exit in exits:
            completion = self._finalize_exit(worker_exit)
            if completion is not None:
                completed.append(completion)
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
        completed = list(self.collect())
        try:
            exits = self._workers.shutdown()
        except Exception:
            self._workers.close_exchanges()
            raise
        try:
            for worker_exit in exits:
                completion = self._finalize_exit(worker_exit)
                if completion is not None:
                    completed.append(completion)
        finally:
            self._workers.close_exchanges()
        return tuple(completed)

    def _prepare_callback(
        self,
        callback: DueOperation,
        monotonic_now: float,
    ) -> bool:
        if self._workers.codex_transition_active():
            self._reject_callback(callback, "callback_authority_busy")
            return False
        try:
            preempted = self._workers.preempt_for_callback(
                callback,
                monotonic_now=monotonic_now,
            )
        except WorkerLaunchError:
            self._reject_callback(callback, "callback_authority_busy")
            return False
        if preempted is None:
            return True
        completion = self._finalize_exit(preempted)
        if completion is not None:
            return True
        self._reject_callback(callback, "callback_authority_busy")
        return False

    def _reject_callback(
        self,
        callback: DueOperation,
        code: str,
    ) -> None:
        removed = False
        try:
            self._queue.remove(
                callback.operation_id,
                expected_state=OperationState.SCHEDULED,
            )
            removed = True
        except ManagedStateConflictError:
            pass
        finally:
            self._workers.cancel_exchange(callback.operation_id)
        if removed:
            self._events.failed(callback, code)

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

    def _finalize_exit(
        self,
        worker_exit: WorkerExit,
    ) -> SchedulerCompletion | None:
        try:
            completion = self._complete_exit(worker_exit)
        except Exception:
            self._workers.complete_exchange(
                worker_exit.operation.operation_id,
                WorkerOutcome.TRANSIENT_FAILURE,
            )
            self._events.failed(
                worker_exit.operation,
                "scheduler_result_failed",
            )
            return None
        self._workers.complete_exchange(
            worker_exit.operation.operation_id,
            completion.outcome,
        )
        self._events.completed(completion)
        return completion

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
        if operation.kind is OperationKind.CODEX_CALLBACK:
            self._queue.remove(
                operation.operation_id,
                expected_state=OperationState.RUNNING,
            )
            state: OperationState | None = None
        elif result.outcome is WorkerOutcome.SUCCEEDED:
            if operation.priority is OperationPriority.SCHEDULED:
                updated = self._queue.transition(
                    operation.operation_id,
                    OperationState.SCHEDULED,
                    updated_at=now,
                    due_at=now + _SCHEDULE_INTERVAL,
                )
                state = updated.state
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
