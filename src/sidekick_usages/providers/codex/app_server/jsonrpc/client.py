"""Transport-independent bounded Codex JSON-RPC correlation."""

import time
from collections import deque
from collections.abc import Callable

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    MAX_JSON_RPC_INTEGER,
    decode_json_rpc_message,
    encode_json_rpc_message,
    validated_json_rpc_error,
    validated_json_rpc_method,
    validated_server_request_id,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    JsonRpcTransport,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.types import (
    JsonRpcMessage,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.serialization.json import JsonObject

_MAX_PENDING_MESSAGES = 16


class JsonRpcClient:
    """Correlate strict requests over one complete-message transport."""

    def __init__(
        self,
        transport: JsonRpcTransport,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._monotonic = monotonic
        self._pending: deque[JsonRpcMessage] = deque()
        self._next_request_id = 1

    @property
    def next_request_id(self) -> int:
        """Return the next monotonically allocated client request ID."""
        return self._next_request_id

    @property
    def closed(self) -> bool:
        """Return whether the framed transport is closed."""
        return self._transport.closed

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonObject:
        """Send one request and return its correlated object result."""
        deadline = self._deadline(timeout_seconds)
        request_id = self._next_request_id
        if request_id > MAX_JSON_RPC_INTEGER:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        self._next_request_id += 1
        try:
            self._send(
                {
                    "id": request_id,
                    "method": validated_json_rpc_method(method),
                    "params": params,
                },
                deadline,
            )
            while True:
                message = self._receive(deadline)
                if isinstance(
                    message,
                    JsonRpcNotification | JsonRpcServerRequest,
                ):
                    self._queue(message)
                    continue
                if message.request_id != request_id:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_MALFORMED
                    )
                if isinstance(message, JsonRpcErrorResponse):
                    raise CodexAppServerError(
                        CodexAppServerFailure.REQUEST_REJECTED
                    )
                return message.result
        except CodexAppServerError:
            self.close()
            raise

    def notify(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> None:
        """Send one client notification within a monotonic deadline."""
        try:
            payload: JsonObject = {"method": validated_json_rpc_method(method)}
            if params is not None:
                payload["params"] = params
            self._send(payload, self._deadline(timeout_seconds))
        except CodexAppServerError:
            self.close()
            raise

    def respond(
        self,
        request_id: int | str,
        result: JsonObject,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> None:
        """Answer one validated server request with an object result."""
        try:
            validated_server_request_id(request_id)
            self._send(
                {"id": request_id, "result": result},
                self._deadline(timeout_seconds),
            )
        except CodexAppServerError:
            self.close()
            raise

    def respond_error(
        self,
        request_id: int | str,
        code: int,
        message: str,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> None:
        """Answer one server request with a fixed safe error."""
        try:
            validated_server_request_id(request_id)
            self._send(
                {
                    "id": request_id,
                    "error": validated_json_rpc_error(code, message),
                },
                self._deadline(timeout_seconds),
            )
        except CodexAppServerError:
            self.close()
            raise

    def receive(
        self,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonRpcMessage:
        """Receive one queued or newly read typed message."""
        if self._pending:
            return self._pending.popleft()
        try:
            return self._receive(self._deadline(timeout_seconds))
        except CodexAppServerError as error:
            if error.code is not CodexAppServerFailure.PROTOCOL_TIMEOUT:
                self.close()
            raise

    def close(self) -> None:
        """Close the framed transport."""
        self._transport.close()

    def _deadline(self, timeout_seconds: float) -> float:
        if timeout_seconds <= 0:
            raise ValueError("JSON-RPC timeout must be positive.")
        return self._monotonic() + timeout_seconds

    def _send(self, payload: JsonObject, deadline: float) -> None:
        self._transport.send(encode_json_rpc_message(payload), deadline)

    def _receive(self, deadline: float) -> JsonRpcMessage:
        return decode_json_rpc_message(self._transport.receive(deadline))

    def _queue(self, message: JsonRpcMessage) -> None:
        if len(self._pending) >= _MAX_PENDING_MESSAGES:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        self._pending.append(message)
