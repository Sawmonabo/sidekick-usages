"""Strict bounded Codex app-server JSON-RPC message codec."""

from typing import NoReturn

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
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
)

MAX_JSON_RPC_MESSAGE_BYTES = 1024 * 1024
_MAX_METHOD_BYTES = 256
_MAX_SERVER_REQUEST_ID_BYTES = 256
_UNICODE_CONTROL_LIMIT = 0x20
_MAX_UNIX_TIMESTAMP_MILLISECONDS = (1 << 63) - 1


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
    if isinstance(request_id, bool):
        return _malformed()
    if isinstance(request_id, int):
        return request_id
    if isinstance(request_id, str):
        try:
            encoded = request_id.encode("utf-8")
        except UnicodeEncodeError:
            return _malformed()
        if (
            not request_id
            or len(encoded) > _MAX_SERVER_REQUEST_ID_BYTES
            or any(
                ord(character) < _UNICODE_CONTROL_LIMIT
                for character in request_id
            )
        ):
            return _malformed()
        return request_id
    return _malformed()


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


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
