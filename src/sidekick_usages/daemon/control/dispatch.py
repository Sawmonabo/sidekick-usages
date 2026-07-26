"""Durable control dispatch and sanitized event fan-out."""

from collections import deque
from collections.abc import Callable, Iterator
from threading import Condition

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.daemon.control.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    ActivationPayload,
    CompletedPayload,
    ControlEvent,
    ControlRequest,
    FailedPayload,
    ProgressPayload,
    ProviderPayload,
    ServiceStoppingPayload,
    SnapshotPayload,
)
from sidekick_usages.daemon.models.scheduler import (
    OperationUpdate,
    SchedulerCompletion,
)
from sidekick_usages.daemon.types.ports import OperationEventSink
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
    ProgressPhase,
    RequestKind,
    ServiceStopReason,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore

_MAX_RETAINED_UPDATES = 512


class OperationEventHub(OperationEventSink):
    """Bounded condition-driven operation and subscription updates."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._updates: deque[OperationUpdate] = deque(
            maxlen=_MAX_RETAINED_UPDATES
        )
        self._sequence = 0
        self._cancelled: set[RequestId] = set()

    def started(self, operation: DueOperation) -> None:
        """Publish one safe running notification."""
        self._append(
            operation.operation_id,
            phase=ProgressPhase.STARTING,
        )

    def completed(self, completion: SchedulerCompletion) -> None:
        """Publish one terminal result after its queue commit."""
        self._append(
            completion.operation_id,
            completion=completion,
        )

    def failed(self, operation: DueOperation, code: str) -> None:
        """Publish one safe terminal coordination failure."""
        self.completed(
            SchedulerCompletion(
                operation_id=operation.operation_id,
                state=(
                    None
                    if operation.kind is OperationKind.CODEX_CALLBACK
                    else OperationState.RETRY_WAIT
                ),
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                failure_code=code,
            )
        )

    def follow_operation(
        self,
        request_id: RequestId,
        operation_id: OperationId,
    ) -> Iterator[OperationUpdate]:
        """Yield matching retained and future updates until terminal."""
        cursor = 0
        try:
            while True:
                update = self._next_update(
                    request_id,
                    cursor,
                    operation_id=operation_id,
                )
                if update is None:
                    return
                cursor = update.sequence
                yield update
                if update.completion is not None:
                    return
        finally:
            self._clear_cancellation(request_id)

    def subscribe(
        self,
        request_id: RequestId,
    ) -> Iterator[OperationUpdate]:
        """Yield future sanitized operation updates until cancellation."""
        with self._condition:
            cursor = self._sequence
        try:
            while True:
                update = self._next_update(request_id, cursor)
                if update is None:
                    return
                cursor = update.sequence
                yield update
        finally:
            self._clear_cancellation(request_id)

    def cancel(self, request_id: RequestId) -> None:
        """Cancel only the disconnected event stream, not durable work."""
        with self._condition:
            self._cancelled.add(request_id)
            self._condition.notify_all()

    def _append(
        self,
        operation_id: OperationId,
        *,
        phase: ProgressPhase | None = None,
        completion: SchedulerCompletion | None = None,
    ) -> None:
        with self._condition:
            self._sequence += 1
            self._updates.append(
                OperationUpdate(
                    self._sequence,
                    operation_id,
                    phase,
                    completion,
                )
            )
            self._condition.notify_all()

    def _next_update(
        self,
        request_id: RequestId,
        cursor: int,
        *,
        operation_id: OperationId | None = None,
    ) -> OperationUpdate | None:
        with self._condition:
            while request_id not in self._cancelled:
                update = next(
                    (
                        candidate
                        for candidate in self._updates
                        if candidate.sequence > cursor
                        and (
                            operation_id is None
                            or candidate.operation_id == operation_id
                        )
                    ),
                    None,
                )
                if update is not None:
                    return update
                self._condition.wait()
            return None

    def _clear_cancellation(self, request_id: RequestId) -> None:
        with self._condition:
            self._cancelled.discard(request_id)


class SupervisorDispatcher:
    """Persist closed control actions before acknowledging them."""

    def __init__(
        self,
        queue: OperationQueueStore,
        service_state: ServiceStateStore,
        events: OperationEventHub,
        clock: Clock,
        wake: Callable[[], None],
        request_stop: Callable[[], None],
        *,
        operation_id_factory: Callable[[], OperationId] = new_operation_id,
        package_version: str = __version__,
    ) -> None:
        self._queue = queue
        self._service_state = service_state
        self._events = events
        self._clock = clock
        self._wake = wake
        self._request_stop = request_stop
        self._operation_id_factory = operation_id_factory
        self._package_version = package_version

    def dispatch(self, request: ControlRequest) -> Iterator[ControlEvent]:
        """Yield events for one already-authenticated closed request."""
        if request.kind in {
            RequestKind.ACTIVATE,
            RequestKind.REFRESH_ACCOUNT,
        }:
            yield from self._dispatch_account(request)
            return
        if request.kind is RequestKind.SNAPSHOT:
            yield self._snapshot(request)
            return
        if request.kind is RequestKind.SUBSCRIBE:
            yield self._event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id=None),
            )
            for update in self._events.subscribe(request.request_id):
                yield self._update_event(request, update)
            return
        if request.kind is RequestKind.REFRESH_ALL:
            yield from self._dispatch_refresh_all(request)
            return
        if request.kind is RequestKind.RECONCILE:
            yield from self._dispatch_reconcile(request)
            return
        if request.kind is RequestKind.SHUTDOWN:
            yield self._event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id=None),
            )
            self._request_stop()
            yield self._event(
                request,
                EventKind.SERVICE_STOPPING,
                ServiceStoppingPayload(ServiceStopReason.REQUESTED),
            )
            return
        yield self._event(
            request,
            EventKind.FAILED,
            FailedPayload(None, "dispatch_failed"),
        )

    def cancel(self, request_id: RequestId) -> None:
        """Cancel one disconnected stream without cancelling durable work."""
        self._events.cancel(request_id)

    def _dispatch_account(
        self,
        request: ControlRequest,
    ) -> Iterator[ControlEvent]:
        payload = request.payload
        if isinstance(payload, ActivationPayload):
            kind = OperationKind.ACTIVATE
            allow_remote_control_disconnect = (
                payload.allow_remote_control_disconnect
            )
        elif isinstance(payload, AccountPayload):
            kind = OperationKind.REFRESH
            allow_remote_control_disconnect = False
        else:
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(None, "dispatch_failed"),
            )
            return
        now = self._clock.now()
        effective = self._queue.enqueue(
            DueOperation(
                operation_id=self._operation_id_factory(),
                provider_id=payload.provider_id,
                account_id=payload.account_id,
                kind=kind,
                priority=OperationPriority.INTERACTIVE,
                state=OperationState.SCHEDULED,
                due_at=now,
                updated_at=now,
                allow_remote_control_disconnect=(
                    allow_remote_control_disconnect
                ),
            )
        )
        self._wake()
        yield self._event(
            request,
            EventKind.ACCEPTED,
            AcceptedPayload(effective.operation_id),
        )
        for update in self._events.follow_operation(
            request.request_id,
            effective.operation_id,
        ):
            yield self._update_event(request, update)

    def _dispatch_refresh_all(
        self,
        request: ControlRequest,
    ) -> Iterator[ControlEvent]:
        now = self._clock.now()
        maintenance = tuple(
            operation
            for operation in self._queue.load()
            if operation.kind is OperationKind.MAINTAIN
        )
        for operation in maintenance:
            self._queue.enqueue(
                DueOperation(
                    operation_id=self._operation_id_factory(),
                    provider_id=operation.provider_id,
                    account_id=operation.required_account_id,
                    kind=OperationKind.MAINTAIN,
                    priority=OperationPriority.SCHEDULED,
                    state=OperationState.SCHEDULED,
                    due_at=now,
                    updated_at=now,
                )
            )
        self._wake()
        yield self._event(
            request,
            EventKind.ACCEPTED,
            AcceptedPayload(operation_id=None),
        )
        yield self._event(
            request,
            EventKind.COMPLETED,
            CompletedPayload(
                None,
                (
                    CompletionOutcome.SUCCEEDED
                    if maintenance
                    else CompletionOutcome.NO_CHANGE
                ),
            ),
        )

    def _dispatch_reconcile(
        self,
        request: ControlRequest,
    ) -> Iterator[ControlEvent]:
        payload = request.payload
        if not isinstance(payload, ProviderPayload):
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(None, "dispatch_failed"),
            )
            return
        now = self._clock.now()
        operation = self._queue.enqueue(
            DueOperation(
                operation_id=self._operation_id_factory(),
                provider_id=payload.provider_id,
                account_id=None,
                kind=OperationKind.RECONCILE_NATIVE,
                priority=OperationPriority.INTERACTIVE,
                state=OperationState.SCHEDULED,
                due_at=now,
                updated_at=now,
            )
        )
        self._wake()
        yield self._event(
            request,
            EventKind.ACCEPTED,
            AcceptedPayload(operation.operation_id),
        )
        for update in self._events.follow_operation(
            request.request_id,
            operation.operation_id,
        ):
            yield self._update_event(request, update)

    def _snapshot(self, request: ControlRequest) -> ControlEvent:
        state = self._service_state.load()
        return self._event(
            request,
            EventKind.SNAPSHOT,
            SnapshotPayload(
                revision=0 if state is None else state.revision,
                ready=(
                    state is not None and state.phase is ServicePhase.READY
                ),
            ),
        )

    def _update_event(
        self,
        request: ControlRequest,
        update: OperationUpdate,
    ) -> ControlEvent:
        if update.phase is not None:
            return self._event(
                request,
                EventKind.PROGRESS,
                ProgressPayload(update.operation_id, update.phase),
            )
        completion = update.completion
        if completion is None:
            raise ValueError("Terminal operation update is incomplete.")
        if completion.outcome in {
            WorkerOutcome.SUCCEEDED,
            WorkerOutcome.NO_CHANGE,
        }:
            return self._event(
                request,
                EventKind.COMPLETED,
                CompletedPayload(
                    completion.operation_id,
                    (
                        CompletionOutcome.SUCCEEDED
                        if completion.outcome is WorkerOutcome.SUCCEEDED
                        else CompletionOutcome.NO_CHANGE
                    ),
                ),
            )
        if completion.outcome is WorkerOutcome.CANCELLED:
            return self._event(
                request,
                EventKind.COMPLETED,
                CompletedPayload(
                    completion.operation_id,
                    CompletionOutcome.CANCELLED,
                ),
            )
        return self._event(
            request,
            EventKind.FAILED,
            FailedPayload(
                completion.operation_id,
                completion.failure_code or "worker_failed",
            ),
        )

    def _event(
        self,
        request: ControlRequest,
        kind: EventKind,
        payload: (
            AcceptedPayload
            | CompletedPayload
            | FailedPayload
            | ProgressPayload
            | ServiceStoppingPayload
            | SnapshotPayload
        ),
    ) -> ControlEvent:
        return ControlEvent(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            kind=kind,
            payload=payload,
            package_version=self._package_version,
        )
