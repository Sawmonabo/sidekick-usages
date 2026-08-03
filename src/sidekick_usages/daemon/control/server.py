"""Authenticated local control server and socket lifecycle."""

import errno
import os
import selectors
import socket
import stat
import sys
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread

from sidekick_usages import __version__
from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.daemon.control.endpoint import (
    RUNTIME_DIRECTORY_MODE,
    SOCKET_MODE,
    runtime_directory_owned,
    socket_owned,
)
from sidekick_usages.daemon.control.protocol import (
    MAX_REQUESTS_PER_CONNECTION,
    UNATTRIBUTED_REQUEST_ID,
    ConnectionClosedError,
    FramedTransport,
    ProtocolFailureError,
)
from sidekick_usages.daemon.models.control import (
    SocketIdentity,
    VerifiedControlRequest,
)
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    ControlEvent,
    ControlRequest,
    EventPayload,
    FailedPayload,
    IncompatiblePayload,
)
from sidekick_usages.daemon.types.control import (
    ControlFailurePhase,
    EndpointFailureCode,
)
from sidekick_usages.daemon.types.ports import (
    ControlDispatcher,
)
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    EventKind,
    ProtocolErrorCode,
    RequestKind,
)
from sidekick_usages.platform.models import PeerIdentity
from sidekick_usages.platform.peer import (
    OperatingSystemPeerVerifier,
    PeerVerificationError,
)
from sidekick_usages.platform.types import PeerVerifier

# Two streams for each of 16 participants per provider, plus four operators.
MAX_CONTROL_CONNECTIONS = 68
_MONITOR_WAKE = b"\x00"
_MONITOR_WAKE_BYTES = 4096
_STREAM_REQUEST_KINDS = frozenset(
    {RequestKind.SUBSCRIBE, RequestKind.PARTICIPANT_SUBSCRIBE}
)


@dataclass(frozen=True, slots=True)
class ControlSubscription:
    """One authenticated stream watched for client-side closure."""

    connection: socket.socket
    context: VerifiedControlRequest
    cancellation_started: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)
    cancellation_failed: Event = field(default_factory=Event)
    diagnostic_degraded: Event = field(default_factory=Event)
    cancellation_lock: Lock = field(
        default_factory=Lock,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class _SubscriptionCommand:
    subscription: ControlSubscription
    register: bool


class ControlSubscriptionMonitor:
    """Wake blocked streams from one bounded selector-owned monitor."""

    def __init__(
        self,
        dispatcher: ControlDispatcher,
        *,
        failure_reporter: Callable[[ControlFailurePhase], None] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._failure_reporter = failure_reporter
        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._writer.setblocking(False)
        self._commands: deque[_SubscriptionCommand] = deque()
        self._lock = Lock()
        self._closing = False
        self._thread = Thread(
            target=self._run,
            daemon=True,
            name="sidekick-subscriptions",
        )

    def start(self) -> None:
        """Start the one shared selector thread."""
        self._thread.start()

    def register(self, subscription: ControlSubscription) -> None:
        """Watch one accepted stream without consuming protocol bytes."""
        self._enqueue(_SubscriptionCommand(subscription, True))

    def unregister(self, subscription: ControlSubscription) -> None:
        """Stop watching one stream that already completed."""
        self._enqueue(_SubscriptionCommand(subscription, False))

    def cancel(self, subscription: ControlSubscription) -> None:
        """Cancel one exact stream through the fault-isolated boundary."""
        self._cancel_subscription(subscription)

    def close(self) -> None:
        """Cancel watched streams and close the shared wake channel."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
        self._notify()
        self._thread.join()
        self._reader.close()
        self._writer.close()

    def _enqueue(self, command: _SubscriptionCommand) -> None:
        with self._lock:
            if self._closing:
                return
            self._commands.append(command)
        self._notify()

    def _notify(self) -> None:
        with suppress(BlockingIOError, OSError):
            self._writer.send(_MONITOR_WAKE)

    def _run(self) -> None:
        selector = selectors.DefaultSelector()
        subscriptions: dict[socket.socket, ControlSubscription] = {}
        selector.register(self._reader, selectors.EVENT_READ)
        try:
            while True:
                for key, _mask in selector.select():
                    if key.fileobj is self._reader:
                        self._drain_wake()
                        self._apply_commands(selector, subscriptions)
                    else:
                        connection = key.fileobj
                        if isinstance(connection, socket.socket):
                            self._cancel(
                                selector,
                                subscriptions,
                                connection,
                            )
                with self._lock:
                    if self._closing:
                        break
        finally:
            for subscription in tuple(subscriptions.values()):
                self._cancel_subscription(subscription)
            selector.close()

    def _apply_commands(
        self,
        selector: selectors.BaseSelector,
        subscriptions: dict[socket.socket, ControlSubscription],
    ) -> None:
        with self._lock:
            commands = tuple(self._commands)
            self._commands.clear()
        failed_registrations: dict[socket.socket, ControlSubscription] = {}
        for command in commands:
            connection = command.subscription.connection
            if command.register:
                if connection not in subscriptions:
                    try:
                        selector.register(connection, selectors.EVENT_READ)
                    except KeyError, OSError, ValueError:
                        failed_registrations[connection] = command.subscription
                    else:
                        failed_registrations.pop(connection, None)
                        subscriptions[connection] = command.subscription
                continue
            failed_registrations.pop(connection, None)
            if connection in subscriptions:
                subscriptions.pop(connection)
                with suppress(KeyError, OSError, ValueError):
                    selector.unregister(connection)
        for subscription in failed_registrations.values():
            self._cancel_subscription(subscription)

    def _cancel(
        self,
        selector: selectors.BaseSelector,
        subscriptions: dict[socket.socket, ControlSubscription],
        connection: socket.socket,
    ) -> None:
        subscription = subscriptions.pop(connection, None)
        with suppress(KeyError, OSError, ValueError):
            selector.unregister(connection)
        if subscription is not None:
            self._cancel_subscription(subscription)

    def _cancel_subscription(
        self,
        subscription: ControlSubscription,
    ) -> None:
        with subscription.cancellation_lock:
            if subscription.cancellation_started.is_set():
                return
            subscription.cancellation_started.set()
        try:
            self._dispatcher.cancel(subscription.context)
        except Exception:
            subscription.cancellation_failed.set()
            reporter = self._failure_reporter
            if reporter is not None:
                try:
                    reporter(ControlFailurePhase.SUBSCRIPTION_CANCELLATION)
                except Exception:
                    subscription.diagnostic_degraded.set()
        else:
            subscription.cancelled.set()

    def _drain_wake(self) -> None:
        while True:
            try:
                if not self._reader.recv(_MONITOR_WAKE_BYTES):
                    return
            except BlockingIOError:
                return


class EndpointError(OSError):
    """The local control endpoint cannot be created safely."""

    def __init__(self, code: EndpointFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class _InvalidDispatchEventError(RuntimeError):
    """Stop one stream after emitting its safe protocol failure."""


def cleanup_control_endpoint(
    runtime_directory: Path,
    socket_path: Path,
) -> None:
    """Remove only an inactive Sidekick socket and its empty directory."""
    if socket_path.parent != runtime_directory:
        raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)
    try:
        metadata = runtime_directory.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise EndpointError(
            EndpointFailureCode.UNSAFE_RUNTIME_DIRECTORY
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise EndpointError(EndpointFailureCode.UNSAFE_RUNTIME_DIRECTORY)
    _remove_inactive_socket(socket_path)
    try:
        runtime_directory.rmdir()
    except OSError:
        return


class ControlConnection:
    """Serve one peer-proven, bounded local control connection."""

    def __init__(
        self,
        connection: socket.socket,
        peer_verifier: PeerVerifier,
        dispatcher: ControlDispatcher,
        *,
        subscription_monitor: ControlSubscriptionMonitor | None = None,
        package_version: str = __version__,
    ) -> None:
        self._connection = connection
        self._peer_verifier = peer_verifier
        self._dispatcher = dispatcher
        self._subscription_monitor = subscription_monitor
        self._package_version = package_version

    def serve(self) -> None:
        """Authenticate first, negotiate versions, then dispatch actions."""
        try:
            peer = self._peer_verifier.verify(self._connection)
        except PeerVerificationError:
            self._connection.close()
            return

        transport = FramedTransport(self._connection)
        try:
            received = self._receive_request(transport)
            if received is None:
                return
            handshake, attachment = received
            if attachment is not None:
                attachment.close()
                self._send_failure(
                    transport,
                    handshake.request_id,
                    ProtocolErrorCode.MALFORMED_FRAME,
                )
                return
            if handshake.kind is not RequestKind.HANDSHAKE:
                self._send_failure(
                    transport,
                    handshake.request_id,
                    ProtocolErrorCode.HANDSHAKE_REQUIRED,
                )
                return
            incompatibility = self._incompatibility(handshake)
            if incompatibility is not None:
                self._send_incompatible(
                    transport,
                    handshake.request_id,
                    incompatibility,
                )
                return
            transport.send_event(
                self.event(
                    handshake.request_id,
                    EventKind.ACCEPTED,
                    AcceptedPayload(operation_id=None),
                )
            )
            self._serve_actions(transport, peer)
        except BrokenPipeError, ConnectionClosedError, ConnectionResetError:
            return
        finally:
            transport.close()

    def event(
        self,
        request_id: RequestId,
        kind: EventKind,
        payload: EventPayload,
    ) -> ControlEvent:
        """Create one service-owned event envelope."""
        return ControlEvent(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            kind=kind,
            payload=payload,
            package_version=self._package_version,
        )

    def _serve_actions(
        self,
        transport: FramedTransport,
        peer: PeerIdentity,
    ) -> None:
        request_count = 1
        while True:
            received = self._receive_request(transport)
            if received is None:
                return
            request, attachment = received
            request_count += 1
            if self._reject_action(
                transport,
                request,
                attachment,
                request_count,
            ):
                return
            if not self._dispatch(
                transport,
                request,
                peer,
                attachment,
            ):
                return

    def _reject_action(
        self,
        transport: FramedTransport,
        request: ControlRequest,
        attachment: socket.socket | None,
        request_count: int,
    ) -> bool:
        error: ProtocolErrorCode | None = None
        if attachment is not None and (
            request.kind is not RequestKind.PARTICIPANT_REGISTER
        ):
            error = ProtocolErrorCode.MALFORMED_FRAME
        elif request_count > MAX_REQUESTS_PER_CONNECTION:
            error = ProtocolErrorCode.TOO_MANY_REQUESTS
        elif request.kind is RequestKind.HANDSHAKE:
            error = ProtocolErrorCode.DISPATCH_FAILED
        incompatibility = self._incompatibility(request)
        if error is None and incompatibility is None:
            return False
        if attachment is not None:
            attachment.close()
        if error is None and incompatibility is not None:
            self._send_incompatible(
                transport,
                request.request_id,
                incompatibility,
            )
        elif error is not None:
            self._send_failure(transport, request.request_id, error)
        return True

    def _receive_request(
        self,
        transport: FramedTransport,
    ) -> tuple[ControlRequest, socket.socket | None] | None:
        try:
            return transport.receive_request_with_attachment()
        except ProtocolFailureError as error:
            with suppress(OSError):
                self._send_failure(
                    transport,
                    UNATTRIBUTED_REQUEST_ID,
                    error.code,
                )
            return None

    def _dispatch(
        self,
        transport: FramedTransport,
        request: ControlRequest,
        peer: PeerIdentity,
        protected_endpoint: socket.socket | None,
    ) -> bool:
        if not self._attachment_matches(
            transport,
            request.request_id,
            peer,
            protected_endpoint,
        ):
            return False
        context = VerifiedControlRequest(
            request,
            peer,
            protected_endpoint,
        )
        subscription: ControlSubscription | None = None
        events = self._dispatcher.dispatch(context)
        try:
            for event in events:
                terminal, accepted = self._send_dispatch_event(
                    transport,
                    request,
                    context,
                    event,
                )
                if accepted is not None:
                    subscription = accepted
                if terminal:
                    return True
            self._send_failure(
                transport,
                request.request_id,
                ProtocolErrorCode.DISPATCH_FAILED,
            )
            return False
        except _InvalidDispatchEventError:
            return False
        except BrokenPipeError, ConnectionResetError:
            self._drain_disconnected_selection(
                request.kind,
                events,
            )
            monitor = self._subscription_monitor
            if subscription is not None and monitor is not None:
                monitor.cancel(subscription)
            else:
                self._dispatcher.cancel(context)
            return False
        except Exception:
            with suppress(OSError):
                self._send_failure(
                    transport,
                    request.request_id,
                    ProtocolErrorCode.DISPATCH_FAILED,
                )
            return False
        finally:
            context.close_protected_endpoint()
            if (
                subscription is not None
                and self._subscription_monitor is not None
            ):
                self._subscription_monitor.unregister(subscription)

    @staticmethod
    def _drain_disconnected_selection(
        kind: RequestKind,
        events: Iterator[ControlEvent],
    ) -> None:
        """Finish server-accepted selection after display disconnects."""
        if kind is not RequestKind.SELECT_ACCOUNT:
            return
        for _event in events:
            pass

    def _attachment_matches(
        self,
        transport: FramedTransport,
        request_id: RequestId,
        peer: PeerIdentity,
        endpoint: socket.socket | None,
    ) -> bool:
        if endpoint is None:
            return True
        try:
            attached_peer = self._peer_verifier.verify(endpoint)
        except PeerVerificationError:
            attached_peer = None
        if attached_peer is not None and (
            attached_peer.process_identity is not None
            and attached_peer.process_identity == peer.process_identity
        ):
            return True
        endpoint.close()
        self._send_failure(
            transport,
            request_id,
            ProtocolErrorCode.DISPATCH_FAILED,
        )
        return False

    def _send_dispatch_event(
        self,
        transport: FramedTransport,
        request: ControlRequest,
        context: VerifiedControlRequest,
        event: ControlEvent,
    ) -> tuple[bool, ControlSubscription | None]:
        """Validate, send, and classify one dispatcher event."""
        if not self._valid_dispatch_event(request, event):
            self._send_failure(
                transport,
                request.request_id,
                ProtocolErrorCode.DISPATCH_FAILED,
            )
            raise _InvalidDispatchEventError
        transport.send_event(event)
        subscription = self._accepted_subscription(
            request,
            context,
            event,
        )
        terminal = event.kind in {
            EventKind.COMPLETED,
            EventKind.FAILED,
            EventKind.INCOMPATIBLE,
            EventKind.SERVICE_STOPPING,
            EventKind.SNAPSHOT,
            EventKind.PARTICIPANT_REGISTERED,
            EventKind.TURN_ADMISSION,
            EventKind.SELECTION_RESULT,
        } or (
            event.kind is EventKind.SELECTION_STATUS
            and request.kind is RequestKind.SELECTION_STATUS
        )
        return terminal, subscription

    def _accepted_subscription(
        self,
        request: ControlRequest,
        context: VerifiedControlRequest,
        event: ControlEvent,
    ) -> ControlSubscription | None:
        monitor = self._subscription_monitor
        if (
            event.kind is not EventKind.ACCEPTED
            or request.kind not in _STREAM_REQUEST_KINDS
            or monitor is None
        ):
            return None
        subscription = ControlSubscription(self._connection, context)
        monitor.register(subscription)
        return subscription

    def _valid_dispatch_event(
        self,
        request: ControlRequest,
        event: ControlEvent,
    ) -> bool:
        return (
            event.request_id == request.request_id
            and event.protocol_version == PROTOCOL_VERSION
            and event.package_version == self._package_version
        )

    def _incompatibility(
        self,
        request: ControlRequest,
    ) -> ProtocolErrorCode | None:
        if request.protocol_version != PROTOCOL_VERSION:
            return ProtocolErrorCode.INCOMPATIBLE_PROTOCOL
        if request.package_version != self._package_version:
            return ProtocolErrorCode.INCOMPATIBLE_VERSION
        return None

    def _send_failure(
        self,
        transport: FramedTransport,
        request_id: RequestId,
        code: ProtocolErrorCode,
    ) -> None:
        transport.send_event(
            self.event(
                request_id,
                EventKind.FAILED,
                FailedPayload(operation_id=None, code=code),
            )
        )

    def _send_incompatible(
        self,
        transport: FramedTransport,
        request_id: RequestId,
        code: ProtocolErrorCode,
    ) -> None:
        transport.send_event(
            self.event(
                request_id,
                EventKind.INCOMPATIBLE,
                IncompatiblePayload(code),
            )
        )


class LocalControlServer:
    """Owner-only Unix socket listener for one per-user supervisor."""

    def __init__(
        self,
        runtime_directory: Path,
        socket_path: Path,
        dispatcher: ControlDispatcher,
        *,
        peer_verifier: PeerVerifier | None = None,
        package_version: str = __version__,
        failure_reporter: Callable[[ControlFailurePhase], None] | None = None,
    ) -> None:
        self._runtime_directory = runtime_directory
        self._socket_path = socket_path
        self._dispatcher = dispatcher
        self._peer_verifier = (
            OperatingSystemPeerVerifier()
            if peer_verifier is None
            else peer_verifier
        )
        self._package_version = package_version
        self._failure_reporter = failure_reporter
        self._listener: socket.socket | None = None
        self._socket_identity: SocketIdentity | None = None
        self._subscription_monitor: ControlSubscriptionMonitor | None = None

    def open(self) -> None:
        """Create and listen on one qualified owner-only Unix socket."""
        if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
            raise EndpointError(EndpointFailureCode.FEATURE_DISABLED)
        self._prepare_runtime_directory()
        self._remove_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: SocketIdentity | None = None
        try:
            listener.bind(str(self._socket_path))
            os.chmod(self._socket_path, SOCKET_MODE)
            metadata = self._socket_path.lstat()
            bound_identity = SocketIdentity(
                metadata.st_dev,
                metadata.st_ino,
            )
            if not socket_owned(metadata):
                raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)
            listener.listen(MAX_CONTROL_CONNECTIONS)
        except OSError as error:
            listener.close()
            if bound_identity is not None:
                self._remove_socket(bound_identity)
            if isinstance(error, EndpointError):
                raise
            raise EndpointError(EndpointFailureCode.CREATE_FAILED) from error
        self._listener = listener
        self._socket_identity = bound_identity
        monitor = ControlSubscriptionMonitor(
            self._dispatcher,
            failure_reporter=self._failure_reporter,
        )
        monitor.start()
        self._subscription_monitor = monitor

    def serve_once(self) -> None:
        """Accept and fully serve one local connection."""
        connection = self.accept_connection()
        if connection is None:
            return
        self.serve_connection(connection)

    def serve_connection(self, connection: socket.socket) -> None:
        """Authenticate and serve one already-accepted connection."""
        ControlConnection(
            connection,
            self._peer_verifier,
            self._dispatcher,
            subscription_monitor=self._subscription_monitor,
            package_version=self._package_version,
        ).serve()

    def fileno(self) -> int:
        """Return the open listener descriptor for selector registration."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("The local control server is not open.")
        return listener.fileno()

    def set_nonblocking(self) -> None:
        """Qualify the open listener for selector-owned accept drains."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("The local control server is not open.")
        listener.setblocking(False)

    def accept_connection(self) -> socket.socket | None:
        """Accept one ready connection without decoding it."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("The local control server is not open.")
        try:
            connection, _address = listener.accept()
        except BlockingIOError:
            return None
        return connection

    def close(self) -> None:
        """Close the listener and remove only its exact socket inode."""
        monitor = self._subscription_monitor
        self._subscription_monitor = None
        if monitor is not None:
            monitor.close()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        identity = self._socket_identity
        self._socket_identity = None
        if identity is None:
            return
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_dev == identity.device
            and metadata.st_ino == identity.inode
        ):
            self._socket_path.unlink()

    def _prepare_runtime_directory(self) -> None:
        self._runtime_directory.mkdir(
            mode=RUNTIME_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        try:
            metadata = self._runtime_directory.lstat()
        except OSError:
            raise EndpointError(
                EndpointFailureCode.UNSAFE_RUNTIME_DIRECTORY
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise EndpointError(EndpointFailureCode.UNSAFE_RUNTIME_DIRECTORY)
        if stat.S_IMODE(metadata.st_mode) != RUNTIME_DIRECTORY_MODE:
            os.chmod(self._runtime_directory, RUNTIME_DIRECTORY_MODE)
            hardened = self._runtime_directory.lstat()
            if not runtime_directory_owned(hardened):
                raise EndpointError(
                    EndpointFailureCode.UNSAFE_RUNTIME_DIRECTORY
                )
        if self._socket_path.parent != self._runtime_directory:
            raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)

    def _remove_stale_socket(self) -> None:
        _remove_inactive_socket(self._socket_path)

    def _remove_socket(self, identity: SocketIdentity) -> None:
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_dev == identity.device
            and metadata.st_ino == identity.inode
        ):
            self._socket_path.unlink()


def _remove_inactive_socket(socket_path: Path) -> None:
    try:
        metadata = socket_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(socket_path))
    except OSError as error:
        if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise EndpointError(
                EndpointFailureCode.UNSAFE_SOCKET_PATH
            ) from None
    else:
        raise EndpointError(EndpointFailureCode.SOCKET_IN_USE)
    finally:
        probe.close()
    try:
        current = socket_path.lstat()
    except FileNotFoundError:
        return
    if (
        current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
        or not stat.S_ISSOCK(current.st_mode)
    ):
        raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)
    socket_path.unlink()
