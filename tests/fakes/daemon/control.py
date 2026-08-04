"""Typed control-protocol boundaries for daemon tests."""

import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import OperationId, RequestId
from sidekick_usages.core.selection.models import (
    OpenSelectionOperation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import ParticipantId, SelectionPhase
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.server import (
    ControlConnection,
    ControlSubscription,
    ControlSubscriptionMonitor,
)
from sidekick_usages.daemon.models.control import VerifiedControlRequest
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    EventPayload,
    ProgressPayload,
)
from sidekick_usages.daemon.runtime.diagnostics import (
    ControlFailureDiagnosticSink,
    SanitizedDiagnosticLog,
)
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.models import (
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
)
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.types.control import ControlFailurePhase
from sidekick_usages.daemon.types.ports import (
    ControlDispatcher,
    ResidentService,
)
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
    ProgressPhase,
    RequestKind,
)
from sidekick_usages.persistence.errors import ReplaceFailedError
from sidekick_usages.persistence.supervisor.selection import (
    SelectionOperationStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import PeerIdentity, ProcessIdentity
from sidekick_usages.platform.peer import PeerVerificationError
from sidekick_usages.platform.types import (
    PeerFailureCode,
    PeerSocket,
    PeerVerifier,
    ProcessLiveness,
)
from tests.fakes.daemon.foundation import FoundationState
from tests.support.time import FixedClock

_FRAGMENT_SIZE = 3
_RESPONSE_BUFFER_SIZE = 65_540
_SERVER_JOIN_SECONDS = 2
_SUBSCRIPTION_WAIT_SECONDS = 2
_BLOCKED_RECEIVE_SECONDS = 2


class FragmentingSocket:
    """Send deliberately small fragments through a real socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self.receive_started = Event()

    def recv(self, size: int, /) -> bytes:
        """Receive up to ``size`` bytes from the real socket."""
        self.receive_started.set()
        return self._connection.recv(size)

    def sendall(self, data: bytes, /) -> None:
        """Send the payload in deliberately small fragments."""
        for offset in range(0, len(data), _FRAGMENT_SIZE):
            self._connection.sendall(data[offset : offset + _FRAGMENT_SIZE])

    def settimeout(self, value: float | None, /) -> None:
        """Set the real socket timeout."""
        self._connection.settimeout(value)

    def shutdown(self, how: int, /) -> None:
        """Disable communication in the requested directions."""
        self._connection.shutdown(how)

    def close(self) -> None:
        """Close the real socket."""
        self._connection.close()


def exercise_blocked_stream_cancellation(
    client: ControlClient,
    stream: Iterator[ControlEvent],
    connection: FragmentingSocket,
) -> Exception | None:
    """Close one client only after its reader reaches blocking I/O."""
    failures: list[Exception] = []

    def receive_event() -> None:
        try:
            next(stream)
        except Exception as error:
            failures.append(error)

    connection.receive_started.clear()
    reader = Thread(target=receive_event)
    reader.start()
    if not connection.receive_started.wait(_BLOCKED_RECEIVE_SECONDS):
        raise AssertionError("Control stream did not begin receiving.")
    client.close()
    reader.join(_BLOCKED_RECEIVE_SECONDS)
    if reader.is_alive():
        raise AssertionError("Blocked control receive did not stop.")
    return failures[0] if len(failures) == 1 else None


class VerifiedPeer:
    """Prove one synthetic same-user operating-system peer."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Return one same-user identity."""
        del connection
        return PeerIdentity(1000)


class RejectedPeer:
    """Reject a peer whose operating-system proof is unavailable."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Raise the closed unavailable-proof failure."""
        del connection
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)


@dataclass(slots=True)
class RecordingDispatcher:
    """Record authenticated actions and expose one cancellable stream."""

    requests: list[ControlRequest]
    cancellations: list[RequestId]
    release_subscription: Event

    def dispatch(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]:
        """Yield the closed synthetic stream for one request."""
        request = context.request
        self.requests.append(request)
        if request.kind is RequestKind.REFRESH_ACCOUNT:
            operation_id = OperationId("f619cb29-9f6e-40dd-b35d-cf6a6ed93f79")
            yield _service_event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id),
            )
            yield _service_event(
                request,
                EventKind.PROGRESS,
                ProgressPayload(operation_id, ProgressPhase.VERIFYING),
            )
            yield _service_event(
                request,
                EventKind.COMPLETED,
                CompletedPayload(
                    operation_id,
                    CompletionOutcome.SUCCEEDED,
                ),
            )
            return
        if request.kind is RequestKind.SUBSCRIBE:
            yield _service_event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id=None),
            )
            self.release_subscription.wait(timeout=_SUBSCRIPTION_WAIT_SECONDS)
            yield _service_event(
                request,
                EventKind.PROGRESS,
                ProgressPayload(
                    operation_id=None,
                    phase=ProgressPhase.RUNNING,
                ),
            )
            return
        raise AssertionError("Unexpected synthetic dispatch request.")

    def cancel(self, context: VerifiedControlRequest) -> None:
        """Record one cancelled stream request."""
        self.cancellations.append(context.request.request_id)
        self.release_subscription.set()


class FailingProcessInspector:
    """Fail exact liveness inspection after coordinator disconnect."""

    def __init__(self) -> None:
        self.attempted = Event()

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        """Expose one synthetic downstream inspection failure."""
        del identity
        self.attempted.set()
        raise RuntimeError("synthetic cancellation failure")


class ExactProcessInspectorFake:
    """Return configured liveness for exact process-start identities."""

    def __init__(self) -> None:
        self.dead: set[ProcessIdentity] = set()
        self.dead_after_first: set[ProcessIdentity] = set()
        self.inspected: list[ProcessIdentity] = []

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        """Return death immediately or after one unknown observation."""
        self.inspected.append(identity)
        later_dead = identity in self.dead_after_first and (
            self.inspected.count(identity) > 1)
        if identity in self.dead or later_dead:
            return ProcessLiveness.DEAD
        return ProcessLiveness.UNKNOWN


class ObservedSelectionOperationStore(SelectionOperationStore):
    """Expose exact durable selection phases to concurrent journeys."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.preparing = Event()
        self.awaiting_ready = Event()
        self.crash_after_complete_once = False
        self.block_complete_once = False
        self.complete_started = Event()
        self.allow_complete = Event()
        self.reject_final_snapshot_once = False

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        result = super().compare_and_swap(expected, replacement)
        self._observe_phase(replacement)
        return result

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        if (
            self.reject_final_snapshot_once
            and expected.phase is SelectionPhase.AWAITING_READY
            and replacement.phase is SelectionPhase.AWAITING_READY
            and replacement.ready_participant_ids
        ):
            self.reject_final_snapshot_once = False
            raise ReplaceFailedError
        result = super().advance_with_required_additions(
            expected,
            replacement,
        )
        self._observe_phase(replacement)
        return result

    def complete(self, result: SelectionResult) -> SelectionResult:
        completed = super().complete(result)
        if self.block_complete_once:
            self.block_complete_once = False
            self.complete_started.set()
            if not self.allow_complete.wait(1):
                raise RuntimeError("Synthetic completion gate timed out.")
        if self.crash_after_complete_once:
            self.crash_after_complete_once = False
            raise RuntimeError("Synthetic crash after durable completion.")
        return completed

    def _observe_phase(self, operation: OpenSelectionOperation) -> None:
        if operation.phase is SelectionPhase.PREPARING:
            self.preparing.set()
        if operation.phase is SelectionPhase.AWAITING_READY:
            self.awaiting_ready.set()


class FailingControlReporter:
    """Raise after an optional sanitized diagnostic projection."""

    def __init__(
        self,
        persist: Callable[[ControlFailurePhase], None] | None = None,
    ) -> None:
        self._persist = persist

    def __call__(self, phase: ControlFailurePhase) -> None:
        """Project once, then expose a synthetic diagnostic I/O failure."""
        if self._persist is not None:
            self._persist(phase)
        raise OSError("synthetic diagnostic failure")


@dataclass(frozen=True, slots=True)
class RegistryMonitorResult:
    """Observed state from one production cancellation journey."""

    participant_id: ParticipantId
    registry_state: tuple[
        int,
        int,
        tuple[ParticipantId, ...],
        int,
        int,
    ]
    states: tuple[bool, bool, bool, bool, bool, bool, bool, bool]
    diagnostics: str


class RegistryMonitorScenario:
    """Own sockets and production services for one monitor journey."""

    def __init__(
        self,
        state: FoundationState,
        resident: ResidentService,
    ) -> None:
        self._state = state
        self._resident = resident

    def exercise(self) -> RegistryMonitorResult:
        """Run registration, failed cancellation, and later cancellation."""
        participant_id = ParticipantId("5b78ccdf-5e8b-4054-a86f-e6a052bc742a")
        process = ProcessIdentity(101, 202)
        registry = ParticipantRegistry(self._state.selected)
        registry.register(
            ParticipantManifest(
                participant_id=participant_id,
                provider_id=ProviderId.CLAUDE,
                client_kind=ParticipantClientKind.CLAUDE_CODE,
                capability_version=1,
                connection_generation=1,
            ),
            process,
        )
        participant = ParticipantConnectionRequest(participant_id, 1)
        inspector = FailingProcessInspector()
        selection = SelectionCoordinator(
            self._state.selected,
            SelectionOperationStore(self._state.paths.selection_journals),
            registry,
            SelectionWorkerGateway(
                self._state.queue,
                FixedClock(),
                Event().set,
            ),
            FixedClock(),
            process_inspector=inspector,
        )
        diagnostic = ControlFailureDiagnosticSink(
            SanitizedDiagnosticLog(self._state.paths.service_logs),
            FixedClock(),
        )
        dispatcher = SupervisorDispatcher(
            self._state.queue,
            ServiceStateStore(self._state.paths.service_state),
            OperationEventHub(diagnostic.failed),
            FixedClock(),
            Event().set,
            Event().set,
            selection=selection,
        )
        reporter = FailingControlReporter(diagnostic.failed)
        monitor = ControlSubscriptionMonitor(
            dispatcher,
            failure_reporter=reporter,
        )
        failed_registration, failed_peer = _subscription_pair(
            RequestKind.PARTICIPANT_SUBSCRIBE,
            participant,
            process,
        )
        registry.close_admission(
            ProviderId.CLAUDE,
            OperationId("52bbb5ad-b457-41ce-90ca-c52919051f8e"),
            SelectionEpoch(1),
        )
        stream = dispatcher.dispatch(failed_registration.context)
        accepted = next(stream)
        activated = registry.snapshot(ProviderId.CLAUDE)
        later, later_peer = _subscription_pair(
            RequestKind.SUBSCRIBE, EmptyPayload()
        )
        failed_registration.connection.close()
        monitor.start()
        try:
            monitor.register(failed_registration)
            cancellation_attempted = inspector.attempted.wait(2)
            before_drain = registry.snapshot(ProviderId.CLAUDE)
            drained_count = len(tuple(stream)) if cancellation_attempted else 0
            later_peer.close()
            monitor.register(later)
            later_cancelled = later.cancelled.wait(2)
        finally:
            monitor.close()
            for subscription, peer in (
                (failed_registration, failed_peer),
                (later, later_peer),
            ):
                subscription.connection.close()
                peer.close()
        snapshot = registry.snapshot(ProviderId.CLAUDE)
        return RegistryMonitorResult(
            participant_id=participant_id,
            registry_state=(
                activated.reachable_count,
                before_drain.reachable_count,
                snapshot.unreachable_participant_ids,
                snapshot.reachable_count,
                drained_count,
            ),
            states=(
                accepted.kind is EventKind.ACCEPTED,
                cancellation_attempted,
                failed_registration.cancelled.is_set(),
                failed_registration.cancellation_failed.is_set(),
                failed_registration.diagnostic_degraded.is_set(),
                later_cancelled,
                later.cancellation_failed.is_set(),
                later.diagnostic_degraded.is_set(),
            ),
            diagnostics=(
                self._state.paths.service_logs / "supervisor.jsonl"
            ).read_text(),
        )


def _subscription_pair(
    kind: RequestKind,
    payload: EmptyPayload | ParticipantConnectionRequest,
    process: ProcessIdentity | None = None,
) -> tuple[ControlSubscription, socket.socket]:
    server, client = socket.socketpair()
    request = ControlRequest(
        PROTOCOL_VERSION,
        new_request_id(),
        kind,
        payload,
        __version__,
    )
    return (
        ControlSubscription(
            server,
            VerifiedControlRequest(request, PeerIdentity(1000, process)),
        ),
        client,
    )


def _service_event(
    request: ControlRequest,
    kind: EventKind,
    payload: EventPayload,
) -> ControlEvent:
    return ControlEvent(
        protocol_version=PROTOCOL_VERSION,
        request_id=request.request_id,
        kind=kind,
        payload=payload,
        package_version=__version__,
    )


def foundation_dispatcher(
    state: FoundationState,
) -> SupervisorDispatcher:
    """Compose the default dispatcher for one foundation state graph."""
    return SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        OperationEventHub(),
        FixedClock(),
        Event().set,
        Event().set,
    )


def serve_protocol_connection(
    connection: socket.socket,
    verifier: PeerVerifier,
    dispatcher: ControlDispatcher,
) -> None:
    """Serve one synchronous control connection."""
    monitor = ControlSubscriptionMonitor(dispatcher)
    monitor.start()
    try:
        ControlConnection(
            connection,
            verifier,
            dispatcher,
            subscription_monitor=monitor,
        ).serve()
    finally:
        monitor.close()


def exercise_closed_subscription_monitor(
    dispatcher: ControlDispatcher,
    context: VerifiedControlRequest,
) -> None:
    """Retire a closed stream before queued commands are applied."""
    monitor = ControlSubscriptionMonitor(dispatcher)
    server, client = socket.socketpair()
    subscription = ControlSubscription(server, context)
    monitor.register(subscription)
    monitor.unregister(subscription)
    server.close()
    monitor.start()
    monitor.close()
    client.close()


def rejected_protocol_response(
    verifier: PeerVerifier,
    outbound: bytes,
    dispatcher: RecordingDispatcher,
) -> bytes:
    """Return the complete response after one rejected control exchange."""
    server_socket, client_socket = socket.socketpair()
    client_socket.sendall(outbound)
    server = Thread(
        target=serve_protocol_connection,
        args=(server_socket, verifier, dispatcher),
    )
    server.start()
    server.join(timeout=_SERVER_JOIN_SECONDS)
    assert not server.is_alive()
    response = bytearray()
    while True:
        try:
            chunk = client_socket.recv(_RESPONSE_BUFFER_SIZE)
        except ConnectionResetError:
            break
        if not chunk:
            break
        response.extend(chunk)
    client_socket.close()
    return bytes(response)
