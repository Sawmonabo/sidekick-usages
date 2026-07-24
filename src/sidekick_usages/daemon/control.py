"""Authenticated local connection and socket lifecycle boundaries."""

import errno
import os
import socket
import stat
import sys
from contextlib import suppress
from pathlib import Path

from sidekick_usages import __version__
from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.daemon.models.control import SocketIdentity
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    ControlEvent,
    ControlRequest,
    EventPayload,
    FailedPayload,
    IncompatiblePayload,
)
from sidekick_usages.daemon.peer import (
    OperatingSystemPeerVerifier,
    PeerVerificationError,
)
from sidekick_usages.daemon.protocol import (
    MAX_REQUESTS_PER_CONNECTION,
    PROTOCOL_VERSION,
    UNATTRIBUTED_REQUEST_ID,
    ConnectionClosedError,
    FramedTransport,
    ProtocolFailureError,
)
from sidekick_usages.daemon.types.control import EndpointFailureCode
from sidekick_usages.daemon.types.ports import (
    ControlDispatcher,
    PeerVerifier,
)
from sidekick_usages.daemon.types.protocol import (
    EventKind,
    ProtocolErrorCode,
    RequestKind,
)

__all__ = [
    "ControlConnection",
    "EndpointError",
    "LocalControlServer",
    "cleanup_control_endpoint",
]

_RUNTIME_DIRECTORY_MODE = 0o700
_SOCKET_MODE = 0o600
_LISTEN_BACKLOG = 16


class EndpointError(OSError):
    """The local control endpoint cannot be created safely."""

    def __init__(self, code: EndpointFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


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
        package_version: str = __version__,
    ) -> None:
        self._connection = connection
        self._peer_verifier = peer_verifier
        self._dispatcher = dispatcher
        self._package_version = package_version

    def serve(self) -> None:
        """Authenticate first, negotiate versions, then dispatch actions."""
        try:
            self._peer_verifier.verify(self._connection)
        except PeerVerificationError:
            self._connection.close()
            return

        transport = FramedTransport(self._connection)
        try:
            handshake = self._receive_request(transport)
            if handshake is None:
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
            self._serve_actions(transport)
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

    def _serve_actions(self, transport: FramedTransport) -> None:
        request_count = 1
        while True:
            request = self._receive_request(transport)
            if request is None:
                return
            request_count += 1
            if request_count > MAX_REQUESTS_PER_CONNECTION:
                self._send_failure(
                    transport,
                    request.request_id,
                    ProtocolErrorCode.TOO_MANY_REQUESTS,
                )
                return
            if request.kind is RequestKind.HANDSHAKE:
                self._send_failure(
                    transport,
                    request.request_id,
                    ProtocolErrorCode.DISPATCH_FAILED,
                )
                return
            incompatibility = self._incompatibility(request)
            if incompatibility is not None:
                self._send_incompatible(
                    transport,
                    request.request_id,
                    incompatibility,
                )
                return
            if not self._dispatch(transport, request):
                return

    def _receive_request(
        self,
        transport: FramedTransport,
    ) -> ControlRequest | None:
        try:
            return transport.receive_request()
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
    ) -> bool:
        terminal = False
        try:
            for event in self._dispatcher.dispatch(request):
                if not self._valid_dispatch_event(request, event):
                    self._send_failure(
                        transport,
                        request.request_id,
                        ProtocolErrorCode.DISPATCH_FAILED,
                    )
                    return False
                transport.send_event(event)
                terminal = event.kind in {
                    EventKind.COMPLETED,
                    EventKind.FAILED,
                    EventKind.INCOMPATIBLE,
                    EventKind.SERVICE_STOPPING,
                    EventKind.SNAPSHOT,
                }
                if terminal:
                    return True
            if not terminal:
                self._send_failure(
                    transport,
                    request.request_id,
                    ProtocolErrorCode.DISPATCH_FAILED,
                )
                return False
        except BrokenPipeError, ConnectionResetError:
            self._dispatcher.cancel(request.request_id)
            return False
        except Exception:
            with suppress(OSError):
                self._send_failure(
                    transport,
                    request.request_id,
                    ProtocolErrorCode.DISPATCH_FAILED,
                )
            return False

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
        self._listener: socket.socket | None = None
        self._socket_identity: SocketIdentity | None = None

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
            os.chmod(self._socket_path, _SOCKET_MODE)
            metadata = self._socket_path.lstat()
            bound_identity = SocketIdentity(
                metadata.st_dev,
                metadata.st_ino,
            )
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != _SOCKET_MODE
                or not stat.S_ISSOCK(metadata.st_mode)
            ):
                raise EndpointError(EndpointFailureCode.UNSAFE_SOCKET_PATH)
            listener.listen(_LISTEN_BACKLOG)
        except OSError as error:
            listener.close()
            if bound_identity is not None:
                self._remove_socket(bound_identity)
            if isinstance(error, EndpointError):
                raise
            raise EndpointError(EndpointFailureCode.CREATE_FAILED) from error
        self._listener = listener
        self._socket_identity = bound_identity

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
            package_version=self._package_version,
        ).serve()

    def fileno(self) -> int:
        """Return the open listener descriptor for selector registration."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("The local control server is not open.")
        return listener.fileno()

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
            mode=_RUNTIME_DIRECTORY_MODE,
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
        if stat.S_IMODE(metadata.st_mode) != _RUNTIME_DIRECTORY_MODE:
            os.chmod(self._runtime_directory, _RUNTIME_DIRECTORY_MODE)
            hardened = self._runtime_directory.lstat()
            if (
                stat.S_IMODE(hardened.st_mode) != _RUNTIME_DIRECTORY_MODE
                or hardened.st_uid != os.geteuid()
            ):
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
