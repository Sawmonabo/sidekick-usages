"""Typed control-protocol boundaries for daemon tests."""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event, Thread

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import OperationId, RequestId
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import ParticipantId
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
from sidekick_usages.daemon.selection.models import (
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
)
from sidekick_usages.daemon.selection.ports import SelectionSupervisorPort
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
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
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import PeerIdentity, ProcessIdentity
from sidekick_usages.platform.peer import PeerVerificationError
from sidekick_usages.platform.types import (
    PeerFailureCode,
    PeerSocket,
    PeerVerifier,
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
        if request.kind is RequestKind.ACTIVATE:
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


class FailingRegistrySelection(SelectionSupervisorPort):
    """Fail one cancellation after retaining registry disconnect truth."""

    def __init__(self, registry: ParticipantRegistry) -> None:
        self._registry = registry
        self.attempted = Event()

    def cancel_subscription(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Disconnect exactly, then fail the downstream step."""
        self._registry.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        self._registry.cancel_subscription(request_id)
        self._registry.disconnect(
            request.participant_id,
            request.connection_generation,
        )
        self.attempted.set()
        raise RuntimeError("synthetic cancellation failure")


@dataclass(frozen=True, slots=True)
class RegistryMonitorResult:
    """Observed state from one production cancellation journey."""

    participant_id: ParticipantId
    registry_state: tuple[
        int,
        tuple[ParticipantId, ...],
        tuple[ParticipantNotice, ...],
    ]
    cancellation_states: tuple[bool, bool, bool, bool, bool, bool]
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
        first, first_peer = _subscription_pair(
            RequestKind.PARTICIPANT_SUBSCRIBE,
            participant,
            process,
        )
        notices = registry.subscribe(
            first.context.request.request_id,
            participant,
        )
        next(notices)
        registry.close_admission(ProviderId.CLAUDE, SelectionEpoch(1))
        selection = FailingRegistrySelection(registry)
        diagnostic = ControlFailureDiagnosticSink(
            SanitizedDiagnosticLog(self._state.paths.service_logs),
            FixedClock(),
        )
        dispatcher = SupervisorDispatcher(
            self._state.queue,
            ServiceStateStore(self._state.paths.service_state),
            OperationEventHub(diagnostic.failed),
            self._resident,
            FixedClock(),
            Event().set,
            Event().set,
            selection=selection,
        )
        monitor = ControlSubscriptionMonitor(
            dispatcher,
            failure_reporter=diagnostic.failed,
        )
        failed_registration, failed_peer = _subscription_pair(
            RequestKind.SUBSCRIBE,
            EmptyPayload(),
        )
        later, later_peer = _subscription_pair(
            RequestKind.SUBSCRIBE,
            EmptyPayload(),
        )
        failed_registration.connection.close()
        monitor.start()
        try:
            monitor.register(failed_registration)
            registration_cancelled = failed_registration.cancelled.wait(2)
            first_peer.close()
            monitor.register(first)
            cancellation_attempted = selection.attempted.wait(2)
            observed_notices = tuple(notices) if cancellation_attempted else ()
            later_peer.close()
            monitor.register(later)
            later_cancelled = later.cancelled.wait(2)
        finally:
            monitor.close()
            for subscription, peer in (
                (first, first_peer),
                (failed_registration, failed_peer),
                (later, later_peer),
            ):
                subscription.connection.close()
                peer.close()
        snapshot = registry.snapshot(ProviderId.CLAUDE)
        return RegistryMonitorResult(
            participant_id=participant_id,
            registry_state=(
                snapshot.reachable_count,
                snapshot.unreachable_participant_ids,
                observed_notices,
            ),
            cancellation_states=(
                registration_cancelled,
                cancellation_attempted,
                first.cancelled.is_set(),
                first.cancellation_failed.is_set(),
                later_cancelled,
                later.cancellation_failed.is_set(),
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
