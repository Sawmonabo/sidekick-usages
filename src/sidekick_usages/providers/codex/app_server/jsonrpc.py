"""Bounded Codex app-server JSON-lines correlation."""

import os
import selectors
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import NoReturn

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.models import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.process import (
    CODEX_PROCESS_GRACE_SECONDS,
    start_codex_json_lines,
    terminate_and_reap_codex_process,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    JsonRpcMessage,
)
from sidekick_usages.serialization import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
)

MAX_JSON_RPC_LINE_BYTES = 1024 * 1024
DEFAULT_JSON_RPC_TIMEOUT_SECONDS = 5.0
_MAX_METHOD_BYTES = 256
_MAX_PENDING_MESSAGES = 16
_MAX_REQUEST_ID = (1 << 63) - 1
_MAX_SERVER_REQUEST_ID_BYTES = 256
_READ_CHUNK_BYTES = 64 * 1024
_UNICODE_CONTROL_LIMIT = 0x20

__all__ = [
    "DEFAULT_JSON_RPC_TIMEOUT_SECONDS",
    "MAX_JSON_RPC_LINE_BYTES",
    "JsonRpcConnection",
]


class JsonRpcConnection:
    """One strict request-correlated JSON-lines child connection."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        monotonic: Callable[[], float],
    ) -> None:
        stdout = process.stdout
        stdin = process.stdin
        if stdout is None or stdin is None:
            terminate_and_reap_codex_process(process)
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        self._process = process
        self._monotonic = monotonic
        self._stdout = stdout
        self._stdin = stdin
        self._selector = selectors.DefaultSelector()
        self._buffer = bytearray()
        self._pending: deque[JsonRpcMessage] = deque()
        self._next_request_id = 1
        self._closed = False
        try:
            os.set_blocking(stdout.fileno(), False)
            os.set_blocking(stdin.fileno(), False)
            self._selector.register(stdout, selectors.EVENT_READ)
        except OSError:
            self._selector.close()
            terminate_and_reap_codex_process(process)
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None

    @classmethod
    def open(
        cls,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> JsonRpcConnection:
        """Start one exact JSON-lines subprocess connection."""
        return cls(
            start_codex_json_lines(argv, environment),
            monotonic,
        )

    @property
    def process_id(self) -> int:
        """Return the exact child process identifier."""
        return self._process.pid

    @property
    def next_request_id(self) -> int:
        """Return the next monotonically allocated client request ID."""
        return self._next_request_id

    @property
    def closed(self) -> bool:
        """Return whether the child has been closed and reaped."""
        return self._closed

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
        if request_id > _MAX_REQUEST_ID:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        self._next_request_id += 1
        try:
            self._write(
                {
                    "id": request_id,
                    "method": _validated_method(method),
                    "params": params,
                },
                deadline,
            )
            while True:
                message = self._read_message(deadline)
                if isinstance(message, JsonRpcNotification):
                    self._queue(message)
                    continue
                if isinstance(message, JsonRpcServerRequest):
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
            payload: JsonObject = {"method": _validated_method(method)}
            if params is not None:
                payload["params"] = params
            self._write(payload, self._deadline(timeout_seconds))
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
            _validated_server_request_id(request_id)
            self._write(
                {"id": request_id, "result": result},
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
            return self._read_message(self._deadline(timeout_seconds))
        except CodexAppServerError:
            self.close()
            raise

    def close(self) -> None:
        """Close input, force bounded termination when needed, and reap."""
        if self._closed:
            return
        self._closed = True
        self._selector.close()
        try:
            self._stdin.close()
        except OSError:
            terminate_and_reap_codex_process(self._process)
            return
        try:
            self._process.wait(timeout=CODEX_PROCESS_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            terminate_and_reap_codex_process(self._process)

    def _deadline(self, timeout_seconds: float) -> float:
        if timeout_seconds <= 0:
            raise ValueError("JSON-RPC timeout must be positive.")
        return self._monotonic() + timeout_seconds

    def _write(self, payload: JsonObject, deadline: float) -> None:
        if self._closed:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_CLOSED)
        try:
            encoded = encode_compact_json(payload) + b"\n"
        except JsonEncodeError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            ) from None
        if len(encoded) - 1 > MAX_JSON_RPC_LINE_BYTES:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        view = memoryview(encoded)
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._stdin, selectors.EVENT_WRITE)
            while view:
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_TIMEOUT
                    )
                try:
                    written = os.write(self._stdin.fileno(), view)
                except BrokenPipeError:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_CLOSED
                    ) from None
                if written == 0:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_CLOSED
                    )
                view = view[written:]
        except OSError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_CLOSED
            ) from None
        finally:
            selector.close()

    def _read_message(self, deadline: float) -> JsonRpcMessage:
        payload = self._read_line(deadline)
        try:
            decoded = decode_json_object(payload)
        except InvalidPayloadError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            ) from None
        return _decode_message(decoded)

    def _read_line(self, deadline: float) -> bytes:
        while True:
            line_end = self._buffer.find(b"\n")
            if line_end >= 0:
                line = bytes(self._buffer[:line_end])
                del self._buffer[: line_end + 1]
                if not line or len(line) > MAX_JSON_RPC_LINE_BYTES:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_MALFORMED
                    )
                return line
            if len(self._buffer) > MAX_JSON_RPC_LINE_BYTES:
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_MALFORMED
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_TIMEOUT
                )
            try:
                chunk = os.read(
                    self._stdout.fileno(),
                    min(
                        _READ_CHUNK_BYTES,
                        MAX_JSON_RPC_LINE_BYTES + 1 - len(self._buffer),
                    ),
                )
            except OSError:
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_CLOSED
                ) from None
            if not chunk:
                failure = (
                    CodexAppServerFailure.PROTOCOL_MALFORMED
                    if self._buffer
                    else CodexAppServerFailure.PROTOCOL_CLOSED
                )
                raise CodexAppServerError(failure)
            self._buffer.extend(chunk)

    def _queue(self, message: JsonRpcMessage) -> None:
        if len(self._pending) >= _MAX_PENDING_MESSAGES:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        self._pending.append(message)


def _decode_message(payload: JsonObject) -> JsonRpcMessage:
    if "jsonrpc" in payload:
        _malformed()
    has_id = "id" in payload
    has_method = "method" in payload
    if has_method:
        return _decode_server_call(payload, has_id=has_id)
    if not has_id:
        return _malformed()
    return _decode_response(payload)


def _decode_server_call(
    payload: JsonObject,
    *,
    has_id: bool,
) -> JsonRpcServerRequest | JsonRpcNotification:
    expected_keys = (
        {"id", "method", "params"} if has_id else {"method", "params"}
    )
    if set(payload) != expected_keys:
        return _malformed()
    method = _message_method(payload)
    params = _message_params(payload)
    if not has_id:
        return JsonRpcNotification(method, params)
    request_id = _validated_server_request_id(payload["id"])
    return JsonRpcServerRequest(request_id, method, params)


def _decode_response(payload: JsonObject) -> JsonRpcMessage:
    request_id = payload["id"]
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
    ):
        _malformed()
    if set(payload) == {"id", "result"}:
        result = payload["result"]
        if not isinstance(result, dict):
            _malformed()
        return JsonRpcResponse(request_id, result)
    if set(payload) == {"id", "error"}:
        error = payload["error"]
        if not isinstance(error, dict):
            _malformed()
        code = error.get("code")
        message = error.get("message")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or not isinstance(message, str)
        ):
            _malformed()
        return JsonRpcErrorResponse(request_id, code)
    return _malformed()


def _message_method(payload: JsonObject) -> str:
    method = payload["method"]
    if not isinstance(method, str):
        _malformed()
    return _validated_method(method)


def _message_params(payload: JsonObject) -> JsonObject:
    params = payload["params"]
    if not isinstance(params, dict):
        _malformed()
    return params


def _validated_method(method: str) -> str:
    try:
        encoded = method.encode("utf-8")
    except UnicodeEncodeError:
        _malformed()
    if (
        not method
        or len(encoded) > _MAX_METHOD_BYTES
        or any(ord(character) < _UNICODE_CONTROL_LIMIT for character in method)
    ):
        _malformed()
    return method


def _validated_server_request_id(
    request_id: object,
) -> int | str:
    if isinstance(request_id, bool):
        _malformed()
    if isinstance(request_id, int):
        return request_id
    if isinstance(request_id, str):
        try:
            encoded = request_id.encode("utf-8")
        except UnicodeEncodeError:
            _malformed()
        if (
            not request_id
            or len(encoded) > _MAX_SERVER_REQUEST_ID_BYTES
            or any(
                ord(character) < _UNICODE_CONTROL_LIMIT
                for character in request_id
            )
        ):
            _malformed()
        return request_id
    return _malformed()


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
