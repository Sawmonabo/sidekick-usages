"""Bounded framing and exact JSON codec for structured Claude control."""

import socket
from dataclasses import dataclass
from typing import NoReturn

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import OperationKind, ParticipantId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.environment import (
    MAXIMUM_CLAUDE_ENVIRONMENT_VALUE_BYTES,
)
from sidekick_usages.providers.claude.process import (
    MAX_CLAUDE_CONTROL_FRAME_BYTES,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredInstallReceipt,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    clear_mutable_buffer,
)
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
    encode_compact_json_buffer,
)

CLAUDE_AUTH_ENVIRONMENT_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
MAX_CLAUDE_PROTECTED_FRAME_BYTES = 512 * 1024
_RESPONSE_KEYS = frozenset({"response", "type"})
_SUCCESS_KEYS = frozenset({"request_id", "subtype"})
_REJECTION_KEYS = frozenset({"error", "request_id", "subtype"})
_MAXIMUM_REJECTION_ERROR_BYTES = 1024
_MINIMUM_PRINTABLE_ASCII = ord(" ")
_MAXIMUM_PRINTABLE_ASCII = ord("~")
_EXPECTED_REJECTION_ERROR = (
    "update_environment_variables: variables must be an object of string "
    "values"
)
_PROJECTION_PREFIX_BYTES = 4
_SOCKET_READ_BYTES = 64 * 1024
_PROJECTION_KEYS = frozenset(
    {"operation_id", "account_id", "generation", "epoch", "nonce"}
)
_PARTICIPANT_PROJECTION_KEYS = _PROJECTION_KEYS | {
    "participant_id",
    "connection_generation",
}
_INSTALL_RECEIPT_KEYS = _PARTICIPANT_PROJECTION_KEYS | {
    "structured_request_id"
}
_BINDING_QUERY_KEYS = frozenset(
    {"kind", "nonce", "participant_id", "connection_generation"}
)
_BINDING_REPORT_KEYS = _BINDING_QUERY_KEYS | {
    "operation_id",
    "account_id",
    "generation",
    "epoch",
}
_BINDING_QUERY_KIND = "binding_query"
_BINDING_REPORT_KIND = "binding_report"
_WORKER_PROJECTION_KEYS = _PROJECTION_KEYS | {"child_operation_id"}
_EXCHANGE_KEYS = frozenset(
    {
        "child_operation_id",
        "parent_operation_id",
        "provider_id",
        "kind",
        "nonce",
        "response_deadline",
        "completion_deadline",
    }
)
_ACK_KEYS = frozenset({"child_operation_id", "nonce"})


class ClaudeProtectedChannelError(RuntimeError):
    """Reject malformed or replayed protected channel data."""


class ClaudeProtectedChannelClosedError(ClaudeProtectedChannelError):
    """Report a retryable protected transport closure."""


@dataclass(frozen=True, slots=True)
class ClaudeProtectedProjectionMetadata:
    """Secret-free binding and route decoded from one projection."""

    binding: ClaudeStructuredBinding
    nonce: RequestId
    child_operation_id: OperationId | None
    participant_id: ParticipantId | None
    connection_generation: int | None


@dataclass(frozen=True, slots=True)
class ClaudeProtectedExchangeInstruction:
    """Exact safe worker-exchange instruction metadata."""

    child_operation_id: OperationId
    parent_operation_id: OperationId
    provider_id: ProviderId
    kind: OperationKind
    nonce: RequestId
    response_deadline: float
    completion_deadline: float


def encode_protected_exchange_instruction(
    instruction: ClaudeProtectedExchangeInstruction,
) -> bytes:
    """Encode one exact child-aware exchange instruction."""
    return encode_compact_json(
        {
            "child_operation_id": str(instruction.child_operation_id),
            "parent_operation_id": str(instruction.parent_operation_id),
            "provider_id": instruction.provider_id.value,
            "kind": instruction.kind.value,
            "nonce": str(instruction.nonce),
            "response_deadline": instruction.response_deadline,
            "completion_deadline": instruction.completion_deadline,
        }
    )


def decode_protected_exchange_instruction(
    payload: bytes | bytearray,
) -> ClaudeProtectedExchangeInstruction:
    """Decode one exact child-aware exchange instruction."""
    root = _decode_protected_metadata(payload)
    if set(root) != _EXCHANGE_KEYS:
        _malformed_protected()
    try:
        return ClaudeProtectedExchangeInstruction(
            child_operation_id=OperationId(
                _protected_string(root, "child_operation_id")
            ),
            parent_operation_id=OperationId(
                _protected_string(root, "parent_operation_id")
            ),
            provider_id=ProviderId(_protected_string(root, "provider_id")),
            kind=OperationKind(_protected_string(root, "kind")),
            nonce=RequestId(_protected_string(root, "nonce")),
            response_deadline=_protected_number(root, "response_deadline"),
            completion_deadline=_protected_number(
                root,
                "completion_deadline",
            ),
        )
    except ValueError:
        _malformed_protected()


def encode_protected_projection(
    binding: ClaudeStructuredBinding,
    oauth: bytearray,
    nonce: RequestId,
    *,
    child_operation_id: OperationId | None = None,
    participant_id: ParticipantId | None = None,
    connection_generation: int | None = None,
) -> bytearray:
    """Encode one bounded worker or participant OAuth projection."""
    worker_route = child_operation_id is not None
    participant_route = (
        participant_id is not None and connection_generation is not None
    )
    if (
        worker_route == participant_route
        or not oauth
        or len(oauth) > MAX_CLAUDE_PROTECTED_FRAME_BYTES // 2
    ):
        _malformed_protected()
    metadata: JsonObject = {
        "operation_id": str(binding.operation_id),
        "account_id": str(binding.account_id),
        "generation": str(binding.generation),
        "epoch": binding.epoch.value,
        "nonce": str(nonce),
    }
    if child_operation_id is not None:
        metadata["child_operation_id"] = str(child_operation_id)
    if participant_id is not None:
        metadata["participant_id"] = str(participant_id)
    if connection_generation is not None:
        metadata["connection_generation"] = connection_generation
    header = encode_compact_json(metadata)
    payload = bytearray(len(header).to_bytes(_PROJECTION_PREFIX_BYTES, "big"))
    payload.extend(header)
    payload.extend(oauth)
    return payload


def decode_protected_projection(
    payload: bytearray,
) -> tuple[ClaudeProtectedProjectionMetadata, bytearray]:
    """Decode one bounded projection into safe metadata and mutable OAuth."""
    metadata, oauth = _split_protected_projection(payload)
    keys = set(metadata)
    if keys not in {_WORKER_PROJECTION_KEYS, _PARTICIPANT_PROJECTION_KEYS}:
        clear_secret_buffer(oauth)
        _malformed_protected()
    try:
        binding = ClaudeStructuredBinding(
            operation_id=OperationId(
                _protected_string(metadata, "operation_id")
            ),
            account_id=SidekickAccountId(
                _protected_string(metadata, "account_id")
            ),
            generation=AuthorityGeneration(
                _protected_string(metadata, "generation")
            ),
            epoch=SelectionEpoch(_protected_integer(metadata, "epoch")),
        )
        route = ClaudeProtectedProjectionMetadata(
            binding=binding,
            nonce=RequestId(_protected_string(metadata, "nonce")),
            child_operation_id=(
                OperationId(_protected_string(metadata, "child_operation_id"))
                if "child_operation_id" in metadata
                else None
            ),
            participant_id=(
                ParticipantId(_protected_string(metadata, "participant_id"))
                if "participant_id" in metadata
                else None
            ),
            connection_generation=(
                _protected_integer(metadata, "connection_generation")
                if "connection_generation" in metadata
                else None
            ),
        )
        return route, oauth
    except ClaudeProtectedChannelError:
        clear_secret_buffer(oauth)
        raise
    except TypeError, ValueError:
        clear_secret_buffer(oauth)
        _malformed_protected()


def encode_protected_install_receipt(
    receipt: ClaudeStructuredInstallReceipt,
    nonce: RequestId,
    participant_id: ParticipantId,
    connection_generation: int,
) -> bytes:
    """Encode one secret-free exact participant install receipt."""
    return encode_compact_json(
        {
            "operation_id": str(receipt.binding.operation_id),
            "account_id": str(receipt.binding.account_id),
            "generation": str(receipt.binding.generation),
            "epoch": receipt.binding.epoch.value,
            "nonce": str(nonce),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
            "structured_request_id": str(receipt.request_id),
        }
    )


def require_protected_install_receipt(
    payload: bytearray,
    binding: ClaudeStructuredBinding,
    nonce: RequestId,
    participant_id: ParticipantId,
    connection_generation: int,
) -> ClaudeStructuredInstallReceipt:
    """Require one exact secret-free participant install receipt."""
    try:
        root = _decode_protected_metadata(payload)
        expected: JsonObject = {
            "operation_id": str(binding.operation_id),
            "account_id": str(binding.account_id),
            "generation": str(binding.generation),
            "epoch": binding.epoch.value,
            "nonce": str(nonce),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
        }
        if set(root) != _INSTALL_RECEIPT_KEYS or any(
            root.get(name) != value for name, value in expected.items()
        ):
            _malformed_protected()
        try:
            request_id = RequestId(
                _protected_string(root, "structured_request_id")
            )
        except ValueError:
            _malformed_protected()
        return ClaudeStructuredInstallReceipt(
            binding=binding,
            request_id=request_id,
        )
    finally:
        clear_secret_buffer(payload)


def encode_protected_binding_query(
    nonce: RequestId,
    participant_id: ParticipantId,
    connection_generation: int,
) -> bytes:
    """Encode one nonce-correlated current-binding query."""
    return encode_compact_json(
        {
            "kind": _BINDING_QUERY_KIND,
            "nonce": str(nonce),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
        }
    )


def require_protected_binding_query(
    payload: bytearray,
    participant_id: ParticipantId,
    connection_generation: int,
) -> RequestId:
    """Require one exact current-binding query and return its nonce."""
    try:
        root = _decode_protected_metadata(payload)
        if set(root) != _BINDING_QUERY_KEYS or root != {
            "kind": _BINDING_QUERY_KIND,
            "nonce": root.get("nonce"),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
        }:
            _malformed_protected()
        try:
            return RequestId(_protected_string(root, "nonce"))
        except ValueError:
            _malformed_protected()
    finally:
        clear_secret_buffer(payload)


def encode_protected_binding_report(
    binding: ClaudeStructuredBinding | None,
    nonce: RequestId,
    participant_id: ParticipantId,
    connection_generation: int,
) -> bytes:
    """Encode one secret-free exact current-binding report."""
    return encode_compact_json(
        {
            "kind": _BINDING_REPORT_KIND,
            "nonce": str(nonce),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
            "operation_id": (
                None if binding is None else str(binding.operation_id)
            ),
            "account_id": None if binding is None else str(binding.account_id),
            "generation": None if binding is None else str(binding.generation),
            "epoch": None if binding is None else binding.epoch.value,
        }
    )


def require_protected_binding_report(
    payload: bytearray,
    nonce: RequestId,
    participant_id: ParticipantId,
    connection_generation: int,
) -> ClaudeStructuredBinding | None:
    """Require one exact correlated current-binding report."""
    try:
        root = _decode_protected_metadata(payload)
        expected = {
            "kind": _BINDING_REPORT_KIND,
            "nonce": str(nonce),
            "participant_id": str(participant_id),
            "connection_generation": connection_generation,
        }
        if set(root) != _BINDING_REPORT_KEYS or any(
            root.get(name) != value for name, value in expected.items()
        ):
            _malformed_protected()
        values = tuple(
            root.get(name)
            for name in ("operation_id", "account_id", "generation", "epoch")
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            _malformed_protected()
        try:
            return ClaudeStructuredBinding(
                operation_id=OperationId(
                    _protected_string(root, "operation_id")
                ),
                account_id=SidekickAccountId(
                    _protected_string(root, "account_id")
                ),
                generation=AuthorityGeneration(
                    _protected_string(root, "generation")
                ),
                epoch=SelectionEpoch(_protected_integer(root, "epoch")),
            )
        except ValueError:
            _malformed_protected()
    finally:
        clear_secret_buffer(payload)


def encode_protected_ack(
    child_operation_id: OperationId,
    nonce: RequestId,
) -> bytes:
    """Encode one exact safe worker projection acknowledgement."""
    return encode_compact_json(
        {
            "child_operation_id": str(child_operation_id),
            "nonce": str(nonce),
        }
    )


def require_protected_ack(
    payload: bytearray,
    child_operation_id: OperationId,
    nonce: RequestId,
) -> None:
    """Require one exact safe worker projection acknowledgement."""
    try:
        root = _decode_protected_metadata(payload)
        if set(root) != _ACK_KEYS or root != {
            "child_operation_id": str(child_operation_id),
            "nonce": str(nonce),
        }:
            _malformed_protected()
    finally:
        clear_secret_buffer(payload)


def _split_protected_projection(
    payload: bytearray,
) -> tuple[JsonObject, bytearray]:
    if len(payload) <= _PROJECTION_PREFIX_BYTES:
        _malformed_protected()
    header_size = int.from_bytes(payload[:_PROJECTION_PREFIX_BYTES], "big")
    token_offset = _PROJECTION_PREFIX_BYTES + header_size
    if header_size < 1 or token_offset >= len(payload):
        _malformed_protected()
    return (
        _decode_protected_metadata(
            payload[_PROJECTION_PREFIX_BYTES:token_offset]
        ),
        payload[token_offset:],
    )


def _decode_protected_metadata(payload: bytes | bytearray) -> JsonObject:
    try:
        return decode_json_object(payload)
    except InvalidPayloadError, ValueError:
        _malformed_protected()


def _protected_string(metadata: JsonObject, name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str):
        _malformed_protected()
    return value


def _protected_integer(metadata: JsonObject, name: str) -> int:
    value = metadata.get(name)
    if type(value) is not int:
        _malformed_protected()
    return value


def _protected_number(metadata: JsonObject, name: str) -> float:
    value = metadata.get(name)
    if not isinstance(value, int | float) or isinstance(value, bool):
        _malformed_protected()
    return float(value)


def _malformed_protected() -> NoReturn:
    raise ClaudeProtectedChannelError(
        "The protected channel frame is malformed."
    )


def encode_oauth_update(
    request_id: RequestId,
    oauth: bytearray,
) -> bytearray:
    """Encode one exact JSON line without materializing OAuth text."""
    if (
        not oauth
        or len(oauth) > MAXIMUM_CLAUDE_ENVIRONMENT_VALUE_BYTES
        or any(
            value < _MINIMUM_PRINTABLE_ASCII
            or value > _MAXIMUM_PRINTABLE_ASCII
            for value in oauth
        )
    ):
        _malformed()
    escaped = bytearray()
    try:
        frame = encode_compact_json_buffer(
            {
                "request_id": str(request_id),
                "type": "update_environment_variables",
                "variables": {CLAUDE_AUTH_ENVIRONMENT_KEY: ""},
            }
        )
        for value in oauth:
            if value in {0x22, 0x5C}:
                escaped.append(0x5C)
            escaped.append(value)
        marker = b'""}}'
        offset = frame.rfind(marker)
        if offset < 0:
            clear_secret_buffer(frame)
            _malformed()
        frame[offset + 1 : offset + 1] = escaped
    except JsonEncodeError:
        _malformed()
    finally:
        clear_secret_buffer(escaped)
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
                "variables": {CLAUDE_AUTH_ENVIRONMENT_KEY: 7},
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
    if correlated in consumed_request_ids or correlated != request_id:
        _malformed()
    if subtype == "error":
        raise ClaudeStructuredError(
            ClaudeStructuredFailure.OAUTH_UPDATE_REJECTED
        )
    if subtype != "success":
        _malformed()


def decode_control_success(
    payload: bytes,
    request_id: RequestId,
    consumed_request_ids: frozenset[RequestId] = frozenset(),
) -> None:
    """Require one exact, fresh, correlated control success."""
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


def receive_protected_socket_frame(endpoint: socket.socket) -> bytearray:
    """Receive one bounded protected frame from an exact local endpoint."""
    decoder = BoundedFrameDecoder(MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    while True:
        chunk = endpoint.recv(_SOCKET_READ_BYTES)
        if not chunk:
            decoder.finish()
            raise ClaudeProtectedChannelClosedError(
                "The protected participant channel closed."
            )
        frames = decoder.feed(chunk)
        if len(frames) > 1 or (frames and decoder.pending):
            for frame in frames:
                clear_mutable_buffer(frame)
            raise ClaudeProtectedChannelError(
                "The protected participant receipt is malformed."
            )
        if frames:
            return frames[0]


def _malformed() -> NoReturn:
    raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_MALFORMED)
