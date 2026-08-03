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
    SelectionCode,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.control import VerifiedControlRequest
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    ActivationPayload,
    CompletedPayload,
    ControlEvent,
    ControlRequest,
    EventPayload,
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
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantReadyRequest,
    ParticipantRequestError,
    SelectionRequestError,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.ports import SelectionSupervisorPort
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode
from sidekick_usages.daemon.types.ports import (
    OperationEventSink,
    ResidentService,
)
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
    ProgressPhase,
    ProtocolErrorCode,
    RequestKind,
    ServiceStopReason,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import ProcessIdentity

_MAX_RETAINED_UPDATES = 512
_PARTICIPANT_REQUEST_KINDS = frozenset(
    {
        RequestKind.PARTICIPANT_REGISTER,
        RequestKind.PARTICIPANT_SUBSCRIBE,
        RequestKind.TURN_BEGIN,
        RequestKind.TURN_END,
        RequestKind.PARTICIPANT_READY,
        RequestKind.PARTICIPANT_ADOPT,
    }
)


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
                provider_id=operation.provider_id,
                operation_id=operation.operation_id,
                operation_kind=operation.kind,
                state=(
                    None
                    if operation.kind is OperationKind.CODEX_CALLBACK
                    or operation.kind.is_selection_worker
                    else OperationState.RETRY_WAIT
                ),
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                failure_code=code,
            )
        )

    def current_sequence(self) -> int:
        """Return the last committed event sequence."""
        with self._condition:
            return self._sequence

    def follow_operation(
        self,
        request_id: RequestId,
        operation_id: OperationId,
        *,
        after_sequence: int = 0,
    ) -> Iterator[OperationUpdate]:
        """Yield matching retained and future updates until terminal."""
        cursor = after_sequence
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
        resident: ResidentService,
        clock: Clock,
        wake: Callable[[], None],
        request_stop: Callable[[], None],
        *,
        selection: SelectionSupervisorPort | None = None,
        operation_id_factory: Callable[[], OperationId] = new_operation_id,
    ) -> None:
        self._queue = queue
        self._service_state = service_state
        self._events = events
        self._resident = resident
        self._clock = clock
        self._wake = wake
        self._request_stop = request_stop
        self._selection = selection
        self._operation_id_factory = operation_id_factory
        self._package_version = __version__

    def dispatch(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]:
        """Yield events for one already-authenticated closed request."""
        request = context.request
        if request.kind in {
            RequestKind.PARTICIPANT_REGISTER,
            RequestKind.PARTICIPANT_SUBSCRIBE,
            RequestKind.TURN_BEGIN,
            RequestKind.TURN_END,
            RequestKind.PARTICIPANT_READY,
            RequestKind.PARTICIPANT_ADOPT,
            RequestKind.SELECT_ACCOUNT,
            RequestKind.SELECTION_STATUS,
        }:
            yield from self._dispatch_selection(context)
            return
        yield from self._dispatch_legacy(request)

    def _dispatch_legacy(
        self,
        request: ControlRequest,
    ) -> Iterator[ControlEvent]:
        """Dispatch one non-selection control request."""
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

    def cancel(self, context: VerifiedControlRequest) -> None:
        """Cancel one disconnected stream without cancelling durable work."""
        request = context.request
        payload = request.payload
        peer = context.peer.process_identity
        if (
            request.kind is RequestKind.PARTICIPANT_SUBSCRIBE
            and isinstance(payload, ParticipantConnectionRequest)
            and self._selection is not None
            and peer is not None
        ):
            try:
                self._selection.cancel_subscription(
                    request.request_id,
                    payload,
                    peer,
                )
            except ParticipantRequestError, PermissionError, ValueError:
                return
            return
        self._events.cancel(request.request_id)

    def _dispatch_selection(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]:
        request = context.request
        peer = context.peer.process_identity
        if request.kind in _PARTICIPANT_REQUEST_KINDS and peer is None:
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(
                    None,
                    SelectionCode.UNSUPPORTED_SESSION_CAPABILITY.value,
                ),
            )
            return
        selection = self._selection
        if selection is None:
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(None, ProtocolErrorCode.FEATURE_DISABLED.value),
            )
            return
        code = "dispatch_failed"
        try:
            if request.kind in _PARTICIPANT_REQUEST_KINDS and peer is not None:
                yield from self._participant_events(
                    request,
                    selection,
                    peer,
                )
            else:
                yield from self._selection_operator_events(request, selection)
            return
        except SelectionRequestError as error:
            code = error.code.value
        except ParticipantRequestError as error:
            code = error.code.value
        except PermissionError:
            code = SelectionCode.PARTICIPANT_UNREACHABLE.value
        except BufferError, RuntimeError, ValueError:
            code = SelectionCode.SELECTION_RECOVERY_REQUIRED.value
        yield self._event(
            request,
            EventKind.FAILED,
            FailedPayload(None, code),
        )

    def _participant_events(
        self,
        request: ControlRequest,
        selection: SelectionSupervisorPort,
        peer: ProcessIdentity,
    ) -> Iterator[ControlEvent]:
        payload = request.payload
        if isinstance(payload, ParticipantManifest):
            yield self._event(
                request,
                EventKind.PARTICIPANT_REGISTERED,
                selection.register(payload, peer),
            )
            return
        if isinstance(payload, ParticipantConnectionRequest):
            yield self._event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(None),
            )
            for notice in selection.subscribe(
                request.request_id,
                payload,
                peer,
            ):
                yield self._event(
                    request,
                    EventKind.PARTICIPANT_NOTICE,
                    notice,
                )
            return
        yield self._participant_action_event(
            request,
            selection,
            peer,
        )

    def _participant_action_event(
        self,
        request: ControlRequest,
        selection: SelectionSupervisorPort,
        peer: ProcessIdentity,
    ) -> ControlEvent:
        payload = request.payload
        if isinstance(payload, TurnBeginRequest):
            return self._event(
                request,
                EventKind.TURN_ADMISSION,
                selection.begin_turn(payload, peer),
            )
        if isinstance(payload, TurnEndRequest):
            selection.end_turn(payload, peer)
        elif isinstance(payload, ParticipantReadyRequest):
            selection.ready_request(payload, peer)
        elif isinstance(payload, ParticipantAdoptionRequest):
            selection.adopt_request(payload, peer)
        else:
            raise ValueError("Participant request payload is unrelated.")
        return self._selection_completed(request)

    def _selection_operator_events(
        self,
        request: ControlRequest,
        selection: SelectionSupervisorPort,
    ) -> Iterator[ControlEvent]:
        payload = request.payload
        if isinstance(payload, AccountPayload):
            operation_id = self._operation_id_factory()
            yield self._event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id),
            )
            yield self._event(
                request,
                EventKind.SELECTION_RESULT,
                selection.select(
                    operation_id,
                    payload.provider_id,
                    payload.account_id,
                ),
            )
            return
        if isinstance(payload, ProviderPayload):
            yield self._event(
                request,
                EventKind.SELECTION_STATUS,
                selection.status(payload.provider_id),
            )
            return
        raise ValueError("Selection request payload is unrelated.")

    def _selection_completed(self, request: ControlRequest) -> ControlEvent:
        return self._event(
            request,
            EventKind.COMPLETED,
            CompletedPayload(None, CompletionOutcome.SUCCEEDED),
        )

    def _dispatch_account(
        self,
        request: ControlRequest,
    ) -> Iterator[ControlEvent]:
        payload = request.payload
        if isinstance(payload, ActivationPayload):
            kind = OperationKind.ACTIVATE
        elif isinstance(payload, AccountPayload):
            kind = OperationKind.REFRESH
        else:
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(None, "dispatch_failed"),
            )
            return
        if (
            kind is OperationKind.ACTIVATE
            and payload.provider_id is ProviderId.CODEX
            and not self._resident.available
        ):
            yield self._event(
                request,
                EventKind.FAILED,
                FailedPayload(
                    None,
                    self._resident.failure_code
                    or ServiceFailureCode.CODEX_BROKER_UNAVAILABLE.value,
                ),
            )
            return
        now = self._clock.now()
        event_sequence = self._events.current_sequence()
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
            after_sequence=event_sequence,
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
        event_sequence = self._events.current_sequence()
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
            after_sequence=event_sequence,
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
        payload: EventPayload,
    ) -> ControlEvent:
        return ControlEvent(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            kind=kind,
            payload=payload,
            package_version=self._package_version,
        )
