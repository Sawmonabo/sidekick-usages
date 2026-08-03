"""Typed control-protocol boundaries for daemon tests."""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
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
)
from sidekick_usages.daemon.selection.ports import SelectionSupervisorPort
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
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
_CANCELLATION_PARTICIPANT_ID = ParticipantId(
    "5b78ccdf-5e8b-4054-a86f-e6a052bc742a"
)


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


class CancellationFaultDispatcher:
    """Fail one exact cancellation while recording later monitor work."""

    def __init__(self) -> None:
        self.cancellations: list[RequestId] = []
        self.attempted = (Event(), Event(), Event())
        self.participant_unreachable = Event()

    def dispatch(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]:
        """Expose no request path in the monitor-only proof."""
        del context
        return iter(())

    def cancel(self, context: VerifiedControlRequest) -> None:
        """Fail the second cancellation and record every exact attempt."""
        index = len(self.cancellations)
        self.cancellations.append(context.request.request_id)
        if index < len(self.attempted):
            self.attempted[index].set()
        if index == 1:
            self.participant_unreachable.set()
            raise RuntimeError("synthetic cancellation failure")


class _FailingRegistryCancellation(SelectionSupervisorPort):
    """Fail after the production registry retains unreachable truth."""

    def __init__(self, registry: ParticipantRegistry) -> None:
        self._registry = registry

    def cancel_subscription(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Disconnect the exact registry owner before the synthetic fault."""
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
        raise RuntimeError("synthetic downstream cancellation failure")


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


def exercise_subscription_monitor_failures(
    context: VerifiedControlRequest,
    log_root: Path,
) -> None:
    """Prove failed registration and cancellation remain fault-isolated."""
    dispatcher = CancellationFaultDispatcher()
    diagnostic = ControlFailureDiagnosticSink(
        SanitizedDiagnosticLog(log_root),
        FixedClock(),
    )
    monitor = ControlSubscriptionMonitor(
        dispatcher,
        failure_reporter=diagnostic.failed,
    )
    monitor.start()
    subscriptions: list[ControlSubscription] = []
    clients: list[socket.socket] = []
    try:
        failed_server, failed_client = socket.socketpair()
        failed_server.close()
        failed_registration = ControlSubscription(failed_server, context)
        subscriptions.append(failed_registration)
        clients.append(failed_client)
        monitor.register(failed_registration)

        first_server, first_client = socket.socketpair()
        first_client.close()
        failed_cancellation = ControlSubscription(first_server, context)
        subscriptions.append(failed_cancellation)
        monitor.register(failed_cancellation)

        second_server, second_client = socket.socketpair()
        second_client.close()
        later_cancellation = ControlSubscription(second_server, context)
        subscriptions.append(later_cancellation)
        monitor.register(later_cancellation)

        if not all(
            attempt.wait(_SUBSCRIPTION_WAIT_SECONDS)
            for attempt in dispatcher.attempted
        ):
            raise AssertionError("Monitor did not isolate subscription.")
    finally:
        monitor.close()
        for subscription in subscriptions:
            subscription.connection.close()
        for client in clients:
            client.close()
    if len(dispatcher.cancellations) != len(dispatcher.attempted):
        raise AssertionError("Monitor did not attempt each cancellation.")
    if not failed_registration.cancelled.is_set():
        raise AssertionError("Failed registration did not cancel its stream.")
    if (
        not failed_cancellation.cancellation_failed.is_set()
        or failed_cancellation.cancelled.is_set()
    ):
        raise AssertionError("Failed cancellation was reported as success.")
    if not later_cancellation.cancelled.is_set():
        raise AssertionError("Later subscription was not monitored.")
    if not dispatcher.participant_unreachable.is_set():
        raise AssertionError("Failed participant remained falsely reachable.")
    diagnostic_payload = (log_root / "supervisor.jsonl").read_text(
        encoding="utf-8"
    )
    if (
        diagnostic_payload.count(
            ControlFailurePhase.SUBSCRIPTION_CANCELLATION.value
        )
        != 1
        or "synthetic cancellation failure" in diagnostic_payload
    ):
        raise AssertionError("Cancellation failure was not diagnosed once.")


def exercise_registry_cancellation_truth(
    state: FoundationState,
    resident: ResidentService,
    clock: Clock,
) -> None:
    """Prove production dispatch retains registry unreachable truth."""
    registry = ParticipantRegistry(state.selected)
    peer = ProcessIdentity(101, 202)
    manifest = ParticipantManifest(
        participant_id=_CANCELLATION_PARTICIPANT_ID,
        provider_id=ProviderId.CLAUDE,
        client_kind=ParticipantClientKind.CLAUDE_CODE,
        capability_version=1,
        connection_generation=1,
    )
    registry.register(manifest, peer)
    request_id = new_request_id()
    connection = ParticipantConnectionRequest(
        _CANCELLATION_PARTICIPANT_ID,
        1,
    )
    notices = registry.subscribe(request_id, connection)
    next(notices)
    registry.close_admission(ProviderId.CLAUDE, SelectionEpoch(1))
    dispatcher = SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        OperationEventHub(),
        resident,
        clock,
        Event().set,
        Event().set,
        selection=_FailingRegistryCancellation(registry),
    )
    request = ControlRequest(
        PROTOCOL_VERSION,
        request_id,
        RequestKind.PARTICIPANT_SUBSCRIBE,
        connection,
        __version__,
    )
    try:
        dispatcher.cancel(
            VerifiedControlRequest(request, PeerIdentity(1000, peer))
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Synthetic cancellation failure did not escape.")
    snapshot = registry.snapshot(ProviderId.CLAUDE)
    if (
        snapshot.reachable_count != 0
        or snapshot.unreachable_participant_ids
        != (_CANCELLATION_PARTICIPANT_ID,)
    ):
        raise AssertionError("Registry did not retain unreachable truth.")
    if tuple(notices):
        raise AssertionError("Disconnected subscription remained blocked.")


def exercise_control_cancellation_failures(
    context: VerifiedControlRequest,
    state: FoundationState,
    resident: ResidentService,
) -> None:
    """Prove monitor isolation and production registry truth together."""
    exercise_subscription_monitor_failures(context, state.paths.service_logs)
    exercise_registry_cancellation_truth(state, resident, FixedClock())


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
