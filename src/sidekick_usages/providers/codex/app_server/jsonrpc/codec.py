"""Strict bounded Codex app-server JSON-RPC message codec."""

from dataclasses import dataclass, field
from typing import NoReturn

from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.types import (
    JsonRpcMessage,
)
from sidekick_usages.providers.codex.app_server.methods import (
    INITIALIZED_METHOD,
    THREAD_REALTIME_CLOSED_METHOD,
    THREAD_REALTIME_START_METHOD,
    THREAD_REALTIME_STARTED_METHOD,
    TURN_COMPLETED_METHOD,
    TURN_START_METHOD,
    TURN_STARTED_METHOD,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
)

MAX_JSON_RPC_INTEGER = (1 << 63) - 1
MAX_JSON_RPC_MESSAGE_BYTES = 1024 * 1024
_MAX_METHOD_BYTES = 256
_MAX_ERROR_MESSAGE_BYTES = 1024
_MAX_SERVER_REQUEST_ID_BYTES = 256
_MAX_ROUTING_ID_BYTES = 256
_UNICODE_CONTROL_LIMIT = 0x20
_MAX_UNIX_TIMESTAMP_MILLISECONDS = (1 << 63) - 1
_SAFE_RELAY_ERROR_CODE = -32001
_ACCOUNT_MUTATION_MESSAGE = (
    "Use Sidekick saved-account commands for Codex login or logout."
)
_BACKPRESSURE_MESSAGE = "The Sidekick Codex relay queue is full."
_THREAD_ROUTING_METHODS = frozenset(
    {
        THREAD_REALTIME_CLOSED_METHOD,
        THREAD_REALTIME_START_METHOD,
        THREAD_REALTIME_STARTED_METHOD,
        TURN_COMPLETED_METHOD,
        TURN_START_METHOD,
        TURN_STARTED_METHOD,
    }
)
_TURN_ROUTING_METHODS = frozenset({TURN_COMPLETED_METHOD, TURN_STARTED_METHOD})


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonRpcRouting:
    """Bounded routing fields plus the unchanged complete raw frame."""

    raw: bytes = field(repr=False)
    request_id: int | str | None
    method: str | None
    thread_id: str | None
    turn_id: str | None
    error_response: bool


def decode_json_rpc_routing(
    payload: bytes,
    *,
    from_client: bool,
) -> JsonRpcRouting:
    """Extract only strict routing fields from one complete raw frame."""
    if not payload or len(payload) > MAX_JSON_RPC_MESSAGE_BYTES:
        return _malformed()
    try:
        decoded = decode_json_object(payload)
    except InvalidPayloadError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if "method" in decoded:
        return _decode_routing_call(
            decoded,
            payload,
            from_client=from_client,
        )
    return _decode_routing_response(decoded, payload)


def encode_account_mutation_refusal(request_id: int | str) -> bytes:
    """Encode the fixed typed refusal for provider-local auth mutation."""
    return _encode_relay_refusal(
        request_id,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION,
        _ACCOUNT_MUTATION_MESSAGE,
    )


def encode_relay_backpressure_refusal(request_id: int | str) -> bytes:
    """Encode the fixed typed refusal for an exhausted relay queue."""
    return _encode_relay_refusal(
        request_id,
        SelectionCode.ACTIVE_OPERATION_TIMEOUT,
        _BACKPRESSURE_MESSAGE,
    )


def encode_json_rpc_message(payload: JsonObject) -> bytes:
    """Encode one bounded JSON-RPC object without transport framing."""
    try:
        encoded = encode_compact_json(payload)
    except JsonEncodeError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if not encoded or len(encoded) > MAX_JSON_RPC_MESSAGE_BYTES:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    return encoded


def decode_json_rpc_message(payload: bytes) -> JsonRpcMessage:
    """Decode one bounded complete JSON-RPC object."""
    if not payload or len(payload) > MAX_JSON_RPC_MESSAGE_BYTES:
        return _malformed()
    try:
        decoded = decode_json_object(payload)
    except InvalidPayloadError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if "jsonrpc" in decoded:
        return _malformed()
    has_id = "id" in decoded
    has_method = "method" in decoded
    if has_method:
        return _decode_server_call(decoded, has_id=has_id)
    if not has_id:
        return _malformed()
    return _decode_response(decoded)


def validated_json_rpc_method(method: str) -> str:
    """Return one bounded method without Unicode control characters."""
    try:
        encoded = method.encode("utf-8")
    except UnicodeEncodeError:
        return _malformed()
    if (
        not method
        or len(encoded) > _MAX_METHOD_BYTES
        or any(ord(character) < _UNICODE_CONTROL_LIMIT for character in method)
    ):
        return _malformed()
    return method


def validated_server_request_id(request_id: object) -> int | str:
    """Return one bounded integer or text server request identifier."""
    if isinstance(request_id, int):
        if (
            isinstance(request_id, bool)
            or request_id < -MAX_JSON_RPC_INTEGER
            or request_id > MAX_JSON_RPC_INTEGER
        ):
            return _malformed()
        return request_id
    if not isinstance(request_id, str):
        return _malformed()
    try:
        encoded = request_id.encode("utf-8")
    except UnicodeEncodeError:
        return _malformed()
    if (
        not request_id
        or len(encoded) > _MAX_SERVER_REQUEST_ID_BYTES
        or any(
            ord(character) < _UNICODE_CONTROL_LIMIT for character in request_id
        )
    ):
        return _malformed()
    return request_id


def validated_json_rpc_error(code: int, message: str) -> JsonObject:
    """Build one bounded JSON-RPC error without provider data."""
    if (
        isinstance(code, bool)
        or not isinstance(code, int)
        or code < -MAX_JSON_RPC_INTEGER
        or code > MAX_JSON_RPC_INTEGER
    ):
        return _malformed()
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        return _malformed()
    if (
        not encoded
        or len(encoded) > _MAX_ERROR_MESSAGE_BYTES
        or any(
            ord(character) < _UNICODE_CONTROL_LIMIT for character in message
        )
    ):
        return _malformed()
    return {"code": code, "message": message}


def _decode_routing_call(
    payload: JsonObject,
    raw: bytes,
    *,
    from_client: bool,
) -> JsonRpcRouting:
    method = _message_method(payload)
    if from_client and method == INITIALIZED_METHOD:
        if set(payload) != {"method"}:
            return _malformed()
        params: JsonObject = {}
    else:
        params = _message_params(payload)
    request_id: int | str | None = None
    if "id" in payload:
        if set(payload) != {"id", "method", "params"}:
            return _malformed()
        request_id = validated_server_request_id(payload["id"])
    elif from_client:
        expected = (
            {"method"}
            if method == INITIALIZED_METHOD
            else {"method", "params"}
        )
        if set(payload) != expected:
            return _malformed()
    else:
        if set(payload) != {"emittedAtMs", "method", "params"}:
            return _malformed()
        _validated_emitted_at(payload["emittedAtMs"])
    thread_id = _routing_text(params, "threadId")
    if method in _THREAD_ROUTING_METHODS and thread_id is None:
        return _malformed()
    turn_id = None
    if method in _TURN_ROUTING_METHODS:
        turn_id = _nested_turn_id(params)
    return JsonRpcRouting(
        raw=raw,
        request_id=request_id,
        method=method,
        thread_id=thread_id,
        turn_id=turn_id,
        error_response=False,
    )


def _decode_routing_response(
    payload: JsonObject,
    raw: bytes,
) -> JsonRpcRouting:
    if "id" not in payload:
        _malformed()
    request_id = validated_server_request_id(payload["id"])
    error_response = False
    thread_id = None
    turn_id = None
    if set(payload) == {"id", "result"}:
        result = payload["result"]
        if not isinstance(result, dict):
            _malformed()
        thread_id = _optional_nested_routing_id(result, "thread")
        turn_id = _optional_nested_routing_id(result, "turn")
    elif set(payload) == {"id", "error"}:
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
        validated_json_rpc_error(code, message)
        error_response = True
    else:
        _malformed()
    return JsonRpcRouting(
        raw=raw,
        request_id=request_id,
        method=None,
        thread_id=thread_id,
        turn_id=turn_id,
        error_response=error_response,
    )


def _encode_relay_refusal(
    request_id: int | str,
    code: SelectionCode,
    message: str,
) -> bytes:
    validated_request_id = validated_server_request_id(request_id)
    error = validated_json_rpc_error(_SAFE_RELAY_ERROR_CODE, message)
    error["data"] = {"code": code.value}
    return encode_json_rpc_message(
        {"id": validated_request_id, "error": error}
    )


def _decode_server_call(
    payload: JsonObject,
    *,
    has_id: bool,
) -> JsonRpcServerRequest | JsonRpcNotification:
    expected_keys = (
        {"id", "method", "params"}
        if has_id
        else {"emittedAtMs", "method", "params"}
    )
    if set(payload) != expected_keys:
        return _malformed()
    method = _message_method(payload)
    params = _message_params(payload)
    if not has_id:
        emitted_at = payload["emittedAtMs"]
        if (
            isinstance(emitted_at, bool)
            or not isinstance(emitted_at, int)
            or emitted_at < 0
            or emitted_at > _MAX_UNIX_TIMESTAMP_MILLISECONDS
        ):
            return _malformed()
        return JsonRpcNotification(method, params)
    request_id = validated_server_request_id(payload["id"])
    return JsonRpcServerRequest(request_id, method, params)


def _decode_response(payload: JsonObject) -> JsonRpcMessage:
    request_id = payload["id"]
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
        or request_id > MAX_JSON_RPC_INTEGER
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
        return _malformed()
    return validated_json_rpc_method(method)


def _message_params(payload: JsonObject) -> JsonObject:
    params = payload["params"]
    if not isinstance(params, dict):
        return _malformed()
    return params


def _routing_text(params: JsonObject, name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        return _malformed()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return _malformed()
    if (
        not encoded
        or len(encoded) > _MAX_ROUTING_ID_BYTES
        or any(ord(character) < _UNICODE_CONTROL_LIMIT for character in value)
    ):
        return _malformed()
    return value


def _required_routing_text(params: JsonObject, name: str) -> str:
    value = _routing_text(params, name)
    if value is None:
        return _malformed()
    return value


def _nested_turn_id(params: JsonObject) -> str:
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return _malformed()
    return _required_routing_text(turn, "id")


def _optional_nested_routing_id(
    payload: JsonObject,
    name: str,
) -> str | None:
    nested = payload.get(name)
    if nested is None:
        return None
    if not isinstance(nested, dict):
        return _malformed()
    return _required_routing_text(nested, "id")


def _validated_emitted_at(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_UNIX_TIMESTAMP_MILLISECONDS
    ):
        return _malformed()
    return value


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
