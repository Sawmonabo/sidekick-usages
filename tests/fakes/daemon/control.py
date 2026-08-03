"""Typed control-protocol boundaries for daemon tests."""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event, Thread

from sidekick_usages import __version__
from sidekick_usages.core.accounts.types import OperationId, RequestId
from sidekick_usages.daemon.control.client import ControlClient
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
from sidekick_usages.daemon.types.ports import ControlDispatcher
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
    ProgressPhase,
    RequestKind,
)
from sidekick_usages.platform.models import PeerIdentity
from sidekick_usages.platform.peer import PeerVerificationError
from sidekick_usages.platform.types import (
    PeerFailureCode,
    PeerSocket,
    PeerVerifier,
)

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
