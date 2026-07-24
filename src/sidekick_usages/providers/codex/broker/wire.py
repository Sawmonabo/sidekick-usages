"""Bounded WebSocket JSON-RPC over the official Codex Unix socket."""

import logging
import selectors
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from types import TracebackType

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.protocol import State
from websockets.sync.client import ClientConnection, connect

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.initialization import (
    initialize_codex_app_server,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.client import (
    JsonRpcClient,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    MAX_JSON_RPC_MESSAGE_BYTES,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.types import (
    JsonRpcMessage,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import CodexDaemonAuthority
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.serialization.json import JsonObject

DAEMON_WEBSOCKET_URI = "ws://localhost/rpc"
_OPEN_TIMEOUT_SECONDS = 5.0
_CLOSE_TIMEOUT_SECONDS = 1.0
_PING_INTERVAL_SECONDS = 20.0
_PING_TIMEOUT_SECONDS = 20.0
_MAX_QUEUED_FRAMES = 16
_AUTOMATIC_WRITE_TIMEOUT_SECONDS = 5.0
_WRITE_DEADLINE: ContextVar[float | None] = ContextVar(
    "codex_websocket_write_deadline",
    default=None,
)
_WEBSOCKET_LOGGER = logging.Logger(
    "sidekick_usages.codex.websocket",
    level=logging.CRITICAL + 1,
)
_WEBSOCKET_LOGGER.disabled = True


class DeadlineBoundClientConnection(ClientConnection):
    """Write WebSocket frames without changing the receiver's socket mode."""

    def send_data(self) -> None:
        """Flush protocol data through per-call nonblocking Unix writes."""
        if not self.protocol_mutex.locked():
            raise RuntimeError("WebSocket protocol lock is not held.")
        deadline = _WRITE_DEADLINE.get()
        if deadline is None:
            deadline = time.monotonic() + _AUTOMATIC_WRITE_TIMEOUT_SECONDS
        for data in self.protocol.data_to_send():
            if data:
                self._send_before(data, deadline)
                continue
            with suppress(OSError):
                self.socket.shutdown(socket.SHUT_WR)

    def _send_before(self, data: bytes, deadline: float) -> None:
        selector = selectors.DefaultSelector()
        pending = memoryview(data)
        try:
            selector.register(self.socket, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("WebSocket write deadline elapsed.")
                try:
                    written = self.socket.send(
                        pending,
                        socket.MSG_DONTWAIT,
                    )
                except BlockingIOError, InterruptedError:
                    continue
                if written == 0:
                    raise OSError("WebSocket socket stopped accepting data.")
                pending = pending[written:]
        finally:
            selector.close()


class UnixWebSocketJsonRpcTransport:
    """Exchange text JSON messages over one upgraded Unix connection."""

    def __init__(
        self,
        connection: ClientConnection,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connection = connection
        self._monotonic = monotonic
        self._closed = False

    @classmethod
    def open(
        cls,
        connected_socket: socket.socket,
    ) -> UnixWebSocketJsonRpcTransport:
        """Upgrade one already connected and peer-verified Unix socket."""
        try:
            connection = connect(
                DAEMON_WEBSOCKET_URI,
                sock=connected_socket,
                unix=True,
                compression=None,
                proxy=None,
                open_timeout=_OPEN_TIMEOUT_SECONDS,
                ping_interval=_PING_INTERVAL_SECONDS,
                ping_timeout=_PING_TIMEOUT_SECONDS,
                close_timeout=_CLOSE_TIMEOUT_SECONDS,
                max_size=MAX_JSON_RPC_MESSAGE_BYTES,
                max_queue=_MAX_QUEUED_FRAMES,
                user_agent_header=None,
                logger=_WEBSOCKET_LOGGER,
                create_connection=DeadlineBoundClientConnection,
            )
        except OSError, TimeoutError, WebSocketException:
            connected_socket.close()
            raise CodexBrokerError(
                CodexBrokerFailure.CONNECTION_FAILED
            ) from None
        return cls(connection)

    @property
    def closed(self) -> bool:
        """Return whether the WebSocket can no longer exchange messages."""
        return self._closed or self._connection.state is State.CLOSED

    def send(self, payload: bytes, deadline: float) -> None:
        """Send one unfragmented UTF-8 text message before its deadline."""
        if self.closed:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_CLOSED)
        if deadline <= self._monotonic():
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_TIMEOUT)
        deadline_token = _WRITE_DEADLINE.set(deadline)
        try:
            self._connection.send(payload, text=True)
        except ConnectionClosed as error:
            self._closed = True
            if isinstance(error.__cause__, TimeoutError):
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_TIMEOUT
                ) from None
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_CLOSED
            ) from None
        except OSError, WebSocketException:
            self._closed = True
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_CLOSED
            ) from None
        finally:
            _WRITE_DEADLINE.reset(deadline_token)

    def receive(self, deadline: float) -> bytes:
        """Receive one complete bounded text message before its deadline."""
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_TIMEOUT)
        try:
            message = self._connection.recv(timeout=remaining)
        except TimeoutError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_TIMEOUT
            ) from None
        except ConnectionClosed, OSError, WebSocketException:
            self._closed = True
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_CLOSED
            ) from None
        if not isinstance(message, str):
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            )
        try:
            payload = message.encode("utf-8")
        except UnicodeEncodeError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            ) from None
        if not payload or len(payload) > MAX_JSON_RPC_MESSAGE_BYTES:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            )
        return payload

    def close(self) -> None:
        """Close the WebSocket and its owned Unix socket."""
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        except OSError, WebSocketException:
            self._connection.socket.close()


class CodexDaemonSession:
    """One initialized connection to a qualified official daemon."""

    def __init__(
        self,
        connection: JsonRpcClient,
        authority: CodexDaemonAuthority,
    ) -> None:
        self._connection = connection
        self._authority = authority

    @classmethod
    def open(
        cls,
        manager: CodexDaemonManager,
        authority: CodexDaemonAuthority,
    ) -> CodexDaemonSession:
        """Connect, initialize, and revalidate one shared daemon."""
        transport = UnixWebSocketJsonRpcTransport.open(
            manager.connect(authority)
        )
        connection = JsonRpcClient(transport)
        try:
            initialize_codex_app_server(
                connection,
                manager.native_home,
                authority.executable.version,
            )
            manager.revalidate(authority)
        except CodexAppServerError, CodexBrokerError:
            connection.close()
            raise
        return cls(connection, authority)

    @property
    def authority(self) -> CodexDaemonAuthority:
        """Return the exact daemon and socket qualified for this session."""
        return self._authority

    @property
    def closed(self) -> bool:
        """Return whether the daemon connection is closed."""
        return self._connection.closed

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonObject:
        """Send one correlated request to the shared daemon."""
        return self._connection.request(
            method,
            params,
            timeout_seconds=timeout_seconds,
        )

    def receive(
        self,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonRpcMessage:
        """Receive one queued notification or server request."""
        return self._connection.receive(timeout_seconds=timeout_seconds)

    def respond(
        self,
        request_id: int | str,
        result: JsonObject,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> None:
        """Answer one validated daemon request."""
        self._connection.respond(
            request_id,
            result,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        """Close the shared daemon connection."""
        self._connection.close()

    def __enter__(self) -> CodexDaemonSession:
        """Enter the shared daemon session."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always close the shared daemon connection."""
        del exception_type, exception, traceback
        self.close()
