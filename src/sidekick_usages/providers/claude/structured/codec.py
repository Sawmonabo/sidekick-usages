"""Bounded exact JSON-lines codec for structured Claude control."""

from typing import NoReturn

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.environment import (
    MAXIMUM_CLAUDE_ENVIRONMENT_VALUE_BYTES,
)
from sidekick_usages.providers.claude.process import (
    MAX_CLAUDE_CONTROL_FRAME_BYTES,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredError,
    ClaudeStructuredFailure,
)
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json_buffer,
)

CLAUDE_OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
_RESPONSE_KEYS = frozenset({"response", "type"})
_SUCCESS_KEYS = frozenset({"request_id", "subtype"})
_REJECTION_KEYS = frozenset({"error", "request_id", "subtype"})
_MAXIMUM_REJECTION_ERROR_BYTES = 1024
_EXPECTED_REJECTION_ERROR = (
    "update_environment_variables: variables must be an object of string "
    "values"
)


def encode_oauth_update(request_id: RequestId, oauth: str) -> bytearray:
    """Encode one exact mutable OAuth update JSON line."""
    try:
        encoded_oauth = oauth.encode("utf-8")
    except AttributeError, UnicodeEncodeError:
        _malformed()
    if (
        not oauth
        or "\0" in oauth
        or len(encoded_oauth) > MAXIMUM_CLAUDE_ENVIRONMENT_VALUE_BYTES
    ):
        _malformed()
    try:
        frame = encode_compact_json_buffer(
            {
                "request_id": str(request_id),
                "type": "update_environment_variables",
                "variables": {CLAUDE_OAUTH_TOKEN_VARIABLE: oauth},
            }
        )
    except JsonEncodeError:
        _malformed()
    frame.append(ord("\n"))
    if len(frame) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
        clear_secret_buffer(frame)
        _malformed()
    return frame


def encode_invalid_oauth_probe(request_id: RequestId) -> bytearray:
    """Encode the qualified non-string negative capability probe."""
    try:
        frame = encode_compact_json_buffer(
            {
                "request_id": str(request_id),
                "type": "update_environment_variables",
                "variables": {CLAUDE_OAUTH_TOKEN_VARIABLE: 7},
            }
        )
    except JsonEncodeError:
        _malformed()
    frame.append(ord("\n"))
    return frame


def decode_oauth_update_success(
    payload: bytes,
    request_id: RequestId,
    consumed_request_ids: frozenset[RequestId],
) -> None:
    """Require one exact, fresh, correlated success response."""
    subtype, correlated = _decode_control_response(payload)
    if (
        subtype != "success"
        or correlated in consumed_request_ids
        or correlated != request_id
    ):
        _malformed()


def decode_oauth_update_rejection(
    payload: bytes,
    request_id: RequestId,
) -> None:
    """Require the exact correlated negative-probe rejection."""
    subtype, correlated = _decode_control_response(
        payload,
        expected_error=_EXPECTED_REJECTION_ERROR,
    )
    if subtype != "error" or correlated != request_id:
        _malformed()


def decode_control_response_request_id(
    payload: bytes,
) -> RequestId | None:
    """Return one exact control correlation or preserve an event frame."""
    root = _decode_frame(payload)
    if root.get("type") != "control_response":
        return None
    _, correlated = _decode_control_response_root(root)
    return correlated


def _decode_control_response(
    payload: bytes,
    *,
    expected_error: str | None = None,
) -> tuple[str, RequestId]:
    root = _decode_frame(payload)
    return _decode_control_response_root(root, expected_error=expected_error)


def _decode_frame(payload: bytes) -> JsonObject:
    if not payload or len(payload) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
        _malformed()
    try:
        root = decode_json_object(payload)
    except InvalidPayloadError:
        _malformed()
    return root


def _decode_control_response_root(
    root: JsonObject,
    *,
    expected_error: str | None = None,
) -> tuple[str, RequestId]:
    if set(root) != _RESPONSE_KEYS or root.get("type") != "control_response":
        _malformed()
    response = root.get("response")
    if not isinstance(response, dict):
        _malformed()
    received_id = response.get("request_id")
    subtype = response.get("subtype")
    if not isinstance(received_id, str) or not isinstance(subtype, str):
        _malformed()
    if subtype == "success":
        if set(response) != _SUCCESS_KEYS:
            _malformed()
    elif subtype == "error":
        received_error = response.get("error")
        if (
            set(response) != _REJECTION_KEYS
            or not isinstance(received_error, str)
            or not _valid_error(received_error)
            or (
                expected_error is not None and received_error != expected_error
            )
        ):
            _malformed()
    else:
        _malformed()
    try:
        correlated = RequestId(received_id)
    except ValueError:
        _malformed()
    return subtype, correlated


def _valid_error(error: str) -> bool:
    if not error or "\0" in error:
        return False
    try:
        return len(error.encode("utf-8")) <= _MAXIMUM_REJECTION_ERROR_BYTES
    except UnicodeEncodeError:
        return False


def clear_secret_buffer(buffer: bytearray) -> None:
    """Overwrite and release one best-effort secret transport buffer."""
    buffer[:] = bytes(len(buffer))


def _malformed() -> NoReturn:
    raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_MALFORMED)
