"""Strict bounded Codex app-server JSON-RPC message codec."""

import json
import math
import re
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
    MCP_SERVER_STATUS_UPDATED_METHOD,
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
MAX_JSON_RPC_MESSAGE_BYTES = 16 * 1024 * 1024
_MAX_METHOD_BYTES = 256
_MAX_ERROR_MESSAGE_BYTES = 1024
_MAX_SERVER_REQUEST_ID_BYTES = 256
_MAX_ROUTING_ID_BYTES = 256
_MAX_ROUTING_MEMBERS = 128
_MAX_JSON_MEMBER_NAME_BYTES = 256
_MAX_JSON_NUMBER_BYTES = 128
_MAX_JSON_SKIP_DEPTH = 64
_UNICODE_CONTROL_LIMIT = 0x20
_MAX_UNIX_TIMESTAMP_MILLISECONDS = (1 << 63) - 1
_SAFE_RELAY_ERROR_CODE = -32001
_ACCOUNT_MUTATION_MESSAGE = (
    "Use Sidekick saved-account commands for Codex login or logout."
)
_BACKPRESSURE_MESSAGE = "The Sidekick Codex relay queue is full."
_ADMISSION_REFUSAL_MESSAGE = "Sidekick cannot safely admit a new Codex turn."
_THREAD_ROUTING_METHODS = frozenset(
    {
        MCP_SERVER_STATUS_UPDATED_METHOD,
        THREAD_REALTIME_CLOSED_METHOD,
        THREAD_REALTIME_START_METHOD,
        THREAD_REALTIME_STARTED_METHOD,
        TURN_COMPLETED_METHOD,
        TURN_START_METHOD,
        TURN_STARTED_METHOD,
    }
)
_TURN_ROUTING_METHODS = frozenset({TURN_COMPLETED_METHOD, TURN_STARTED_METHOD})
_MCP_STARTUP_STATES = frozenset({"cancelled", "failed", "ready", "starting"})
_TOP_ROUTING_MEMBERS = frozenset(
    {"emittedAtMs", "error", "id", "method", "params", "result"}
)
_JSON_NUMBER_PATTERN = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)

type _RoutingSpan = tuple[int, int]


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonRpcRouting:
    """Bounded routing fields plus the unchanged complete raw frame."""

    raw: bytes = field(repr=False)
    request_id: int | str | None
    method: str | None
    thread_id: str | None
    turn_id: str | None
    mcp_name: str | None
    mcp_status: str | None
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
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _malformed()
    cursor = _RoutingEnvelopeCursor(text)
    keys, fields = cursor.read_object_fields(
        _TOP_ROUTING_MEMBERS,
        max_members=len(_TOP_ROUTING_MEMBERS),
    )
    cursor.require_complete()
    if "method" in keys:
        return _decode_scanned_routing_call(
            text,
            keys,
            fields,
            payload,
            from_client=from_client,
        )
    return _decode_scanned_routing_response(text, keys, fields, payload)


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


def encode_relay_admission_refusal(
    request_id: int | str,
    code: SelectionCode,
) -> bytes:
    """Encode one exact safe admission refusal from the control plane."""
    return _encode_relay_refusal(
        request_id,
        code,
        _ADMISSION_REFUSAL_MESSAGE,
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


class _RoutingEnvelopeCursor:
    """Validate one relay envelope while retaining routing member spans."""

    def __init__(
        self,
        text: str,
        start: int = 0,
        end: int | None = None,
    ) -> None:
        self._text = text
        self._index = start
        self._end = len(text) if end is None else end

    def read_object_fields(
        self,
        selected: frozenset[str],
        *,
        max_members: int = _MAX_ROUTING_MEMBERS,
    ) -> tuple[set[str], dict[str, _RoutingSpan]]:
        """Return member names and spans without materializing values."""
        self._skip_space()
        self._expect("{")
        keys: set[str] = set()
        fields: dict[str, _RoutingSpan] = {}
        self._skip_space()
        if self._take("}"):
            return keys, fields
        while True:
            if len(keys) >= max_members:
                return _malformed()
            key = self._read_member_name()
            if key in keys:
                return _malformed()
            keys.add(key)
            self._skip_space()
            self._expect(":")
            self._skip_space()
            span = self._value_span()
            if key in selected:
                fields[key] = span
            self._skip_space()
            if self._take("}"):
                return keys, fields
            self._expect(",")
            self._skip_space()

    def require_complete(self) -> None:
        """Require only JSON whitespace after the parsed value."""
        self._skip_space()
        if self._index != self._end:
            return _malformed()
        return None

    def _read_member_name(self) -> str:
        span = self._string_span()
        if span[1] - span[0] > (_MAX_JSON_MEMBER_NAME_BYTES * 6) + 2:
            return _malformed()
        value = _decoded_span(self._text, span)
        if not isinstance(value, str):
            return _malformed()
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return _malformed()
        if not encoded or len(encoded) > _MAX_JSON_MEMBER_NAME_BYTES:
            return _malformed()
        return value

    def _value_span(self) -> _RoutingSpan:
        start = self._index
        self._skip_value(0)
        return start, self._index

    def _skip_value(self, depth: int) -> None:
        if depth > _MAX_JSON_SKIP_DEPTH or self._index >= self._end:
            return _malformed()
        character = self._text[self._index]
        if character == '"':
            self._string_span()
        elif character == "{":
            self._skip_object(depth + 1)
        elif character == "[":
            self._skip_array(depth + 1)
        elif character in "-0123456789":
            self._skip_number()
        elif self._text.startswith("true", self._index):
            self._index += 4
        elif self._text.startswith("false", self._index):
            self._index += 5
        elif self._text.startswith("null", self._index):
            self._index += 4
        else:
            return _malformed()
        return None

    def _skip_object(self, depth: int) -> None:
        self._expect("{")
        self._skip_space()
        if self._take("}"):
            return
        while True:
            self._string_span()
            self._skip_space()
            self._expect(":")
            self._skip_space()
            self._skip_value(depth)
            self._skip_space()
            if self._take("}"):
                return
            self._expect(",")
            self._skip_space()

    def _skip_array(self, depth: int) -> None:
        self._expect("[")
        self._skip_space()
        if self._take("]"):
            return
        while True:
            self._skip_value(depth)
            self._skip_space()
            if self._take("]"):
                return
            self._expect(",")
            self._skip_space()

    def _string_span(self) -> _RoutingSpan:
        start = self._index
        self._expect('"')
        while self._index < self._end:
            character = self._text[self._index]
            self._index += 1
            if character == '"':
                return start, self._index
            if ord(character) < _UNICODE_CONTROL_LIMIT:
                return _malformed()
            if character != "\\":
                continue
            if self._index >= self._end:
                return _malformed()
            escape = self._text[self._index]
            self._index += 1
            if escape in '"\\/bfnrt':
                continue
            if escape != "u" or self._index + 4 > self._end:
                return _malformed()
            if any(
                character not in "0123456789abcdefABCDEF"
                for character in self._text[self._index : self._index + 4]
            ):
                return _malformed()
            self._index += 4
        return _malformed()

    def _skip_number(self) -> None:
        start = self._index
        match = _JSON_NUMBER_PATTERN.match(
            self._text,
            self._index,
            self._end,
        )
        if match is None:
            return _malformed()
        self._index = match.end()
        if self._index - start > _MAX_JSON_NUMBER_BYTES:
            return _malformed()
        number = self._text[start : self._index]
        if any(
            character in number for character in ".eE"
        ) and not math.isfinite(float(number)):
            return _malformed()
        return None

    def _skip_space(self) -> None:
        while self._index < self._end and self._text[self._index] in " \t\r\n":
            self._index += 1

    def _expect(self, character: str) -> None:
        if not self._take(character):
            return _malformed()
        return None

    def _take(self, character: str) -> bool:
        if self._index < self._end and self._text[self._index] == character:
            self._index += 1
            return True
        return False


def _object_fields(
    text: str,
    span: _RoutingSpan,
    selected: frozenset[str],
) -> tuple[set[str], dict[str, _RoutingSpan]]:
    cursor = _RoutingEnvelopeCursor(text, *span)
    observed = cursor.read_object_fields(selected)
    cursor.require_complete()
    return observed


def _decoded_span(text: str, span: _RoutingSpan) -> object:
    try:
        return json.loads(text[span[0] : span[1]])
    except json.JSONDecodeError, RecursionError, ValueError:
        return _malformed()


def _span_text(text: str, span: _RoutingSpan) -> str:
    if span[1] - span[0] > (_MAX_ERROR_MESSAGE_BYTES * 6) + 2:
        return _malformed()
    value = _decoded_span(text, span)
    if not isinstance(value, str):
        return _malformed()
    return value


def _required_span_text(
    text: str,
    fields: dict[str, _RoutingSpan],
    name: str,
) -> str:
    span = fields.get(name)
    if span is None:
        return _malformed()
    return _span_text(text, span)


def _validated_routing_text(value: str) -> str:
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


def _optional_span_text(
    text: str,
    fields: dict[str, _RoutingSpan],
    name: str,
) -> str | None:
    span = fields.get(name)
    if span is None:
        return None
    return _validated_routing_text(_span_text(text, span))


def _nullable_span_text(
    text: str,
    fields: dict[str, _RoutingSpan],
    name: str,
) -> str | None:
    span = fields.get(name)
    if span is None or text[span[0] : span[1]] == "null":
        return None
    return _validated_routing_text(_span_text(text, span))


def _span_integer(text: str, span: _RoutingSpan) -> int:
    if span[1] - span[0] > _MAX_JSON_NUMBER_BYTES:
        return _malformed()
    value = _decoded_span(text, span)
    if isinstance(value, bool) or not isinstance(value, int):
        return _malformed()
    return value


def _span_request_id(text: str, span: _RoutingSpan) -> int | str:
    if span[1] - span[0] > (_MAX_SERVER_REQUEST_ID_BYTES * 6) + 2:
        return _malformed()
    return validated_server_request_id(_decoded_span(text, span))


def _span_emitted_at(text: str, span: _RoutingSpan) -> int:
    value = _span_integer(text, span)
    if value < 0 or value > _MAX_UNIX_TIMESTAMP_MILLISECONDS:
        return _malformed()
    return value


def _optional_nested_id(
    text: str,
    fields: dict[str, _RoutingSpan],
    name: str,
) -> str | None:
    span = fields.get(name)
    if span is None:
        return None
    _keys, nested = _object_fields(text, span, frozenset({"id"}))
    return _optional_span_text(text, nested, "id")


def _required_nested_id(
    text: str,
    fields: dict[str, _RoutingSpan],
    name: str,
) -> str:
    value = _optional_nested_id(text, fields, name)
    if value is None:
        return _malformed()
    return value


def _decode_scanned_routing_call(
    text: str,
    keys: set[str],
    fields: dict[str, _RoutingSpan],
    raw: bytes,
    *,
    from_client: bool,
) -> JsonRpcRouting:
    method = validated_json_rpc_method(
        _required_span_text(text, fields, "method")
    )
    if from_client and method == INITIALIZED_METHOD:
        if keys != {"method"}:
            return _malformed()
        params: dict[str, _RoutingSpan] = {}
    else:
        params_span = fields.get("params")
        params = {}
        if params_span is not None and text[params_span[0]] == "{":
            params_keys, params = _object_fields(
                text,
                params_span,
                frozenset({"name", "status", "threadId", "turn"}),
            )
            del params_keys
        elif method in _THREAD_ROUTING_METHODS:
            return _malformed()
    request_id = _scanned_call_request_id(
        text,
        keys,
        fields,
        method,
        from_client=from_client,
    )
    thread_id = _nullable_span_text(text, params, "threadId")
    if method in _THREAD_ROUTING_METHODS and thread_id is None:
        return _malformed()
    turn_id = None
    if method in _TURN_ROUTING_METHODS:
        turn_id = _required_nested_id(text, params, "turn")
    mcp_name = None
    mcp_status = None
    if method == MCP_SERVER_STATUS_UPDATED_METHOD:
        mcp_name = _required_span_text(text, params, "name")
        mcp_status = _required_span_text(text, params, "status")
        if mcp_status not in _MCP_STARTUP_STATES:
            return _malformed()
    return JsonRpcRouting(
        raw=raw,
        request_id=request_id,
        method=method,
        thread_id=thread_id,
        turn_id=turn_id,
        mcp_name=mcp_name,
        mcp_status=mcp_status,
        error_response=False,
    )


def _scanned_call_request_id(
    text: str,
    keys: set[str],
    fields: dict[str, _RoutingSpan],
    method: str,
    *,
    from_client: bool,
) -> int | str | None:
    if "id" in keys:
        if keys not in (
            {"id", "method"},
            {"id", "method", "params"},
        ):
            return _malformed()
        return _span_request_id(text, fields["id"])
    if from_client:
        if keys not in ({"method"}, {"method", "params"}):
            return _malformed()
        return None
    if keys != {"emittedAtMs", "method", "params"}:
        return _malformed()
    _span_emitted_at(text, fields["emittedAtMs"])
    return None


def _decode_scanned_routing_response(
    text: str,
    keys: set[str],
    fields: dict[str, _RoutingSpan],
    raw: bytes,
) -> JsonRpcRouting:
    if "id" not in keys:
        _malformed()
    request_id = _span_request_id(text, fields["id"])
    error_response = False
    thread_id = None
    turn_id = None
    if keys == {"id", "result"}:
        result_span = fields["result"]
        if text[result_span[0]] == "{":
            _result_keys, result = _object_fields(
                text,
                result_span,
                frozenset({"thread", "turn"}),
            )
            thread_id = _optional_nested_id(text, result, "thread")
            turn_id = _optional_nested_id(text, result, "turn")
    elif keys == {"id", "error"}:
        _error_keys, error = _object_fields(
            text,
            fields["error"],
            frozenset({"code", "message"}),
        )
        code_span = error.get("code")
        message_span = error.get("message")
        if code_span is None or message_span is None:
            return _malformed()
        code = _span_integer(text, code_span)
        message = _span_text(text, message_span)
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
        mcp_name=None,
        mcp_status=None,
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


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
