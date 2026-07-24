"""Bounded secret-free protocol for the local supervisor socket."""

import json
import struct
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from sidekick_usages.core.accounts import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 65_536
MAX_PACKAGE_VERSION_BYTES = 128
MAX_REQUESTS_PER_CONNECTION = 128
READ_CHUNK_BYTES = 16_384
UNATTRIBUTED_REQUEST_ID = RequestId("00000000-0000-0000-0000-000000000000")

_FRAME_PREFIX = struct.Struct(">I")
_MAX_INTEGER = (1 << 63) - 1
_MAX_PROTOCOL_VERSION = 65_535
_MIN_PRINTABLE_ASCII = 0x21
_MAX_PRINTABLE_ASCII = 0x7E

type JsonScalar = str | int | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class RequestKind(StrEnum):
    """Closed client request vocabulary."""

    HANDSHAKE = "handshake"
    SNAPSHOT = "snapshot"
    SUBSCRIBE = "subscribe"
    ACTIVATE = "activate"
    REFRESH_ACCOUNT = "refresh_account"
    REFRESH_ALL = "refresh_all"
    RECONCILE = "reconcile"
    SHUTDOWN = "shutdown"


class EventKind(StrEnum):
    """Closed supervisor event vocabulary."""

    ACCEPTED = "accepted"
    SNAPSHOT = "snapshot"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"
    SERVICE_STOPPING = "service_stopping"


class ProgressPhase(StrEnum):
    """Sanitized operation progress phases."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    VERIFYING = "verifying"
    RECONCILING = "reconciling"


class CompletionOutcome(StrEnum):
    """Sanitized successful terminal outcomes."""

    SUCCEEDED = "succeeded"
    NO_CHANGE = "no_change"
    CANCELLED = "cancelled"


class ProtocolErrorCode(StrEnum):
    """Safe protocol failures that never include rejected input."""

    MALFORMED_FRAME = "malformed_frame"
    FRAME_TOO_LARGE = "frame_too_large"
    HANDSHAKE_REQUIRED = "handshake_required"
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    INCOMPATIBLE_VERSION = "incompatible_version"
    TOO_MANY_REQUESTS = "too_many_requests"
    DISPATCH_FAILED = "dispatch_failed"
    FEATURE_DISABLED = "feature_disabled"


class ServiceStopReason(StrEnum):
    """Safe reasons for a supervisor stopping event."""

    REQUESTED = "requested"
    SHUTTING_DOWN = "shutting_down"


class ProtocolFailureError(ValueError):
    """Base class for safe local-protocol failures."""

    def __init__(self, code: ProtocolErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class FrameTooLargeError(ProtocolFailureError):
    """A declared or encoded frame exceeds the protocol limit."""

    def __init__(self) -> None:
        super().__init__(ProtocolErrorCode.FRAME_TOO_LARGE)


class MalformedFrameError(ProtocolFailureError):
    """A frame is truncated or does not match its strict schema."""

    def __init__(self) -> None:
        super().__init__(ProtocolErrorCode.MALFORMED_FRAME)


class ConnectionClosedError(ConnectionError):
    """The peer closed before another complete frame was available."""


@dataclass(frozen=True, slots=True)
class EmptyPayload:
    """Payload for requests that accept no arguments."""


@dataclass(frozen=True, slots=True)
class AccountPayload:
    """One stable account target without a label or credential."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    """One provider target without provider-owned identity."""

    provider_id: ProviderId


type RequestPayload = EmptyPayload | AccountPayload | ProviderPayload


@dataclass(frozen=True, slots=True)
class AcceptedPayload:
    """Acknowledgement after durable acceptance or handshake."""

    operation_id: OperationId | None


@dataclass(frozen=True, slots=True)
class SnapshotPayload:
    """Bounded supervisor snapshot authority marker."""

    revision: int
    ready: bool

    def __post_init__(self) -> None:
        """Require a nonnegative bounded revision."""
        _require_nonnegative_integer(self.revision)


@dataclass(frozen=True, slots=True)
class ProgressPayload:
    """Sanitized progress for one accepted operation or subscription."""

    operation_id: OperationId | None
    phase: ProgressPhase


@dataclass(frozen=True, slots=True)
class CompletedPayload:
    """Sanitized successful terminal result."""

    operation_id: OperationId | None
    outcome: CompletionOutcome


@dataclass(frozen=True, slots=True)
class FailedPayload:
    """Sanitized failed terminal result."""

    operation_id: OperationId | None
    code: ProtocolErrorCode


@dataclass(frozen=True, slots=True)
class IncompatiblePayload:
    """Version incompatibility reported before action dispatch."""

    code: ProtocolErrorCode

    def __post_init__(self) -> None:
        """Restrict the payload to version negotiation failures."""
        if self.code not in {
            ProtocolErrorCode.INCOMPATIBLE_PROTOCOL,
            ProtocolErrorCode.INCOMPATIBLE_VERSION,
        }:
            raise ValueError("Incompatible events require a version failure.")


@dataclass(frozen=True, slots=True)
class ServiceStoppingPayload:
    """Sanitized service shutdown state."""

    reason: ServiceStopReason


type EventPayload = (
    AcceptedPayload
    | SnapshotPayload
    | ProgressPayload
    | CompletedPayload
    | FailedPayload
    | IncompatiblePayload
    | ServiceStoppingPayload
)


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """One strictly typed client request."""

    protocol_version: int
    request_id: RequestId
    kind: RequestKind
    payload: RequestPayload
    package_version: str

    def __post_init__(self) -> None:
        """Validate envelope values and kind-specific payload ownership."""
        _require_protocol_version(self.protocol_version)
        _require_package_version(self.package_version)
        _require_request_payload(self.kind, self.payload)


@dataclass(frozen=True, slots=True)
class ControlEvent:
    """One strictly typed supervisor response or streamed event."""

    protocol_version: int
    request_id: RequestId
    kind: EventKind
    payload: EventPayload
    package_version: str

    def __post_init__(self) -> None:
        """Validate envelope values and kind-specific payload ownership."""
        _require_protocol_version(self.protocol_version)
        _require_package_version(self.package_version)
        _require_event_payload(self.kind, self.payload)


class ConnectedSocket(Protocol):
    """Minimal connected byte-stream socket used by the protocol."""

    def recv(self, size: int, /) -> bytes:
        """Receive at most ``size`` bytes."""

    def sendall(self, data: bytes, /) -> None:
        """Send all bytes or raise an operating-system error."""

    def close(self) -> None:
        """Close this connection."""


class FrameDecoder:
    """Incrementally decode length-prefixed frames with a bounded buffer."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        """Consume one fragment and return every newly complete payload."""
        if not isinstance(chunk, bytes):
            raise TypeError("Protocol chunks must be bytes.")
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < _FRAME_PREFIX.size:
                    break
                (declared,) = _FRAME_PREFIX.unpack(
                    self._buffer[: _FRAME_PREFIX.size]
                )
                del self._buffer[: _FRAME_PREFIX.size]
                if declared == 0:
                    raise MalformedFrameError
                if declared > MAX_FRAME_BYTES:
                    raise FrameTooLargeError
                self._expected = declared
            if len(self._buffer) < self._expected:
                break
            frames.append(bytes(self._buffer[: self._expected]))
            del self._buffer[: self._expected]
            self._expected = None
        if len(self._buffer) > MAX_FRAME_BYTES:
            raise FrameTooLargeError
        return tuple(frames)

    def finish(self) -> None:
        """Reject an incomplete prefix or payload at end of stream."""
        if self._buffer or self._expected is not None:
            raise MalformedFrameError


class FramedTransport:
    """Read and write complete protocol frames on one byte stream."""

    def __init__(self, connection: ConnectedSocket) -> None:
        self._connection = connection
        self._decoder = FrameDecoder()
        self._pending: deque[bytes] = deque()

    def receive_payload(self) -> bytes:
        """Block until one complete payload is available."""
        if self._pending:
            return self._pending.popleft()
        while True:
            chunk = self._connection.recv(READ_CHUNK_BYTES)
            if not chunk:
                self._decoder.finish()
                raise ConnectionClosedError(
                    "The local control connection closed."
                )
            self._pending.extend(self._decoder.feed(chunk))
            if self._pending:
                return self._pending.popleft()

    def receive_request(self) -> ControlRequest:
        """Receive and strictly decode one client request."""
        return decode_request(self.receive_payload())

    def receive_event(self) -> ControlEvent:
        """Receive and strictly decode one supervisor event."""
        return decode_event(self.receive_payload())

    def send_request(self, request: ControlRequest) -> None:
        """Encode and send one request frame."""
        self._connection.sendall(encode_request(request))

    def send_event(self, event: ControlEvent) -> None:
        """Encode and send one event frame."""
        self._connection.sendall(encode_event(event))

    def close(self) -> None:
        """Close the connected stream."""
        self._connection.close()


def new_request_id() -> RequestId:
    """Return one canonical random request correlation ID."""
    return RequestId(str(uuid4()))


def encode_frame(payload: bytes) -> bytes:
    """Prefix one nonempty bounded payload with its big-endian length."""
    if not payload:
        raise MalformedFrameError
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameTooLargeError
    return _FRAME_PREFIX.pack(len(payload)) + payload


def encode_request(request: ControlRequest) -> bytes:
    """Encode one strict request as a complete framed message."""
    root: JsonValue = {
        "client_package_version": request.package_version,
        "kind": request.kind.value,
        "payload": _request_payload_to_json(request.payload),
        "protocol_version": request.protocol_version,
        "request_id": str(request.request_id),
    }
    return encode_frame(_encode_json(root))


def encode_event(event: ControlEvent) -> bytes:
    """Encode one strict event as a complete framed message."""
    root: JsonValue = {
        "kind": event.kind.value,
        "payload": _event_payload_to_json(event.payload),
        "protocol_version": event.protocol_version,
        "request_id": str(event.request_id),
        "service_package_version": event.package_version,
    }
    return encode_frame(_encode_json(root))


def decode_request(payload: bytes) -> ControlRequest:
    """Strictly decode one unframed request payload."""
    root = _require_object(_decode_json(payload))
    _require_exact_keys(
        root,
        {
            "client_package_version",
            "kind",
            "payload",
            "protocol_version",
            "request_id",
        },
    )
    try:
        kind = RequestKind(_require_string(root["kind"]))
        return ControlRequest(
            protocol_version=_require_integer(root["protocol_version"]),
            request_id=RequestId(_require_string(root["request_id"])),
            kind=kind,
            payload=_decode_request_payload(kind, root["payload"]),
            package_version=_require_string(root["client_package_version"]),
        )
    except TypeError, ValueError:
        raise MalformedFrameError from None


def decode_event(payload: bytes) -> ControlEvent:
    """Strictly decode one unframed supervisor event payload."""
    root = _require_object(_decode_json(payload))
    _require_exact_keys(
        root,
        {
            "kind",
            "payload",
            "protocol_version",
            "request_id",
            "service_package_version",
        },
    )
    try:
        kind = EventKind(_require_string(root["kind"]))
        return ControlEvent(
            protocol_version=_require_integer(root["protocol_version"]),
            request_id=RequestId(_require_string(root["request_id"])),
            kind=kind,
            payload=_decode_event_payload(kind, root["payload"]),
            package_version=_require_string(root["service_package_version"]),
        )
    except TypeError, ValueError:
        raise MalformedFrameError from None


def _require_request_payload(
    kind: RequestKind,
    payload: RequestPayload,
) -> None:
    account_kinds = {
        RequestKind.ACTIVATE,
        RequestKind.REFRESH_ACCOUNT,
    }
    provider_kinds = {RequestKind.RECONCILE}
    if kind in account_kinds and isinstance(payload, AccountPayload):
        return
    if kind in provider_kinds and isinstance(payload, ProviderPayload):
        return
    if kind not in account_kinds | provider_kinds and isinstance(
        payload, EmptyPayload
    ):
        return
    raise ValueError("Request kind and payload do not match.")


def _require_event_payload(
    kind: EventKind,
    payload: EventPayload,
) -> None:
    expected: dict[EventKind, type[EventPayload]] = {
        EventKind.ACCEPTED: AcceptedPayload,
        EventKind.SNAPSHOT: SnapshotPayload,
        EventKind.PROGRESS: ProgressPayload,
        EventKind.COMPLETED: CompletedPayload,
        EventKind.FAILED: FailedPayload,
        EventKind.INCOMPATIBLE: IncompatiblePayload,
        EventKind.SERVICE_STOPPING: ServiceStoppingPayload,
    }
    if not isinstance(payload, expected[kind]):
        raise ValueError("Event kind and payload do not match.")


def _request_payload_to_json(payload: RequestPayload) -> JsonValue:
    if isinstance(payload, EmptyPayload):
        return {}
    if isinstance(payload, AccountPayload):
        return {
            "account_id": str(payload.account_id),
            "provider": payload.provider_id.value,
        }
    return {"provider": payload.provider_id.value}


def _event_payload_to_json(payload: EventPayload) -> JsonValue:
    result: dict[str, JsonValue]
    if isinstance(payload, AcceptedPayload):
        result = {
            "operation_id": _optional_operation_id(payload.operation_id)
        }
    elif isinstance(payload, SnapshotPayload):
        result = {"ready": payload.ready, "revision": payload.revision}
    elif isinstance(payload, ProgressPayload):
        result = {
            "operation_id": _optional_operation_id(payload.operation_id),
            "phase": payload.phase.value,
        }
    elif isinstance(payload, CompletedPayload):
        result = {
            "operation_id": _optional_operation_id(payload.operation_id),
            "outcome": payload.outcome.value,
        }
    elif isinstance(payload, FailedPayload):
        result = {
            "code": payload.code.value,
            "operation_id": _optional_operation_id(payload.operation_id),
        }
    elif isinstance(payload, IncompatiblePayload):
        result = {"code": payload.code.value}
    else:
        result = {"reason": payload.reason.value}
    return result


def _decode_request_payload(
    kind: RequestKind,
    value: JsonValue,
) -> RequestPayload:
    root = _require_object(value)
    if kind in {RequestKind.ACTIVATE, RequestKind.REFRESH_ACCOUNT}:
        _require_exact_keys(root, {"account_id", "provider"})
        return AccountPayload(
            provider_id=ProviderId(_require_string(root["provider"])),
            account_id=SidekickAccountId(_require_string(root["account_id"])),
        )
    if kind is RequestKind.RECONCILE:
        _require_exact_keys(root, {"provider"})
        return ProviderPayload(
            provider_id=ProviderId(_require_string(root["provider"]))
        )
    _require_exact_keys(root, set())
    return EmptyPayload()


def _decode_event_payload(
    kind: EventKind,
    value: JsonValue,
) -> EventPayload:
    root = _require_object(value)
    if kind is EventKind.ACCEPTED:
        _require_exact_keys(root, {"operation_id"})
        result: EventPayload = AcceptedPayload(
            operation_id=_decode_optional_operation_id(root["operation_id"])
        )
    elif kind is EventKind.SNAPSHOT:
        _require_exact_keys(root, {"ready", "revision"})
        result = SnapshotPayload(
            revision=_require_integer(root["revision"]),
            ready=_require_boolean(root["ready"]),
        )
    elif kind is EventKind.PROGRESS:
        _require_exact_keys(root, {"operation_id", "phase"})
        result = ProgressPayload(
            operation_id=_decode_optional_operation_id(root["operation_id"]),
            phase=ProgressPhase(_require_string(root["phase"])),
        )
    elif kind is EventKind.COMPLETED:
        _require_exact_keys(root, {"operation_id", "outcome"})
        result = CompletedPayload(
            operation_id=_decode_optional_operation_id(root["operation_id"]),
            outcome=CompletionOutcome(_require_string(root["outcome"])),
        )
    elif kind is EventKind.FAILED:
        _require_exact_keys(root, {"code", "operation_id"})
        result = FailedPayload(
            operation_id=_decode_optional_operation_id(root["operation_id"]),
            code=ProtocolErrorCode(_require_string(root["code"])),
        )
    elif kind is EventKind.INCOMPATIBLE:
        _require_exact_keys(root, {"code"})
        result = IncompatiblePayload(
            code=ProtocolErrorCode(_require_string(root["code"]))
        )
    else:
        _require_exact_keys(root, {"reason"})
        result = ServiceStoppingPayload(
            reason=ServiceStopReason(_require_string(root["reason"]))
        )
    return result


def _optional_operation_id(operation_id: OperationId | None) -> JsonValue:
    return None if operation_id is None else str(operation_id)


def _decode_optional_operation_id(value: JsonValue) -> OperationId | None:
    if value is None:
        return None
    return OperationId(_require_string(value))


def _encode_json(root: JsonValue) -> bytes:
    try:
        return json.dumps(
            root,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        raise MalformedFrameError from None


def _decode_json(payload: bytes) -> JsonValue:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameTooLargeError
        raise MalformedFrameError
    try:
        text = payload.decode("utf-8", errors="strict")
        decoded: JsonValue = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_integer,
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        TypeError,
        ValueError,
    ):
        raise MalformedFrameError from None
    return decoded


def _strict_object(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> JsonValue:
    raise ValueError("Non-finite JSON number.")


def _reject_float(_value: str) -> JsonValue:
    raise ValueError("Floating-point JSON values are unsupported.")


def _parse_integer(value: str) -> int:
    parsed = int(value)
    if parsed < -_MAX_INTEGER or parsed > _MAX_INTEGER:
        raise ValueError("JSON integer is out of range.")
    return parsed


def _require_object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise MalformedFrameError
    return value


def _require_exact_keys(
    value: dict[str, JsonValue],
    expected: set[str],
) -> None:
    if set(value) != expected:
        raise MalformedFrameError


def _require_string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise MalformedFrameError
    return value


def _require_integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedFrameError
    return value


def _require_nonnegative_integer(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_INTEGER
    ):
        raise ValueError("Revision must be a nonnegative bounded integer.")


def _require_boolean(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise MalformedFrameError
    return value


def _require_protocol_version(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_PROTOCOL_VERSION
    ):
        raise ValueError("Protocol version must be a bounded integer.")


def _require_package_version(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("Package version must be a string.")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Package version must be printable ASCII.") from None
    if (
        not encoded
        or len(encoded) > MAX_PACKAGE_VERSION_BYTES
        or any(
            character < _MIN_PRINTABLE_ASCII
            or character > _MAX_PRINTABLE_ASCII
            for character in encoded
        )
    ):
        raise ValueError("Package version must be printable ASCII.")


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_PACKAGE_VERSION_BYTES",
    "MAX_REQUESTS_PER_CONNECTION",
    "PROTOCOL_VERSION",
    "UNATTRIBUTED_REQUEST_ID",
    "AcceptedPayload",
    "AccountPayload",
    "CompletedPayload",
    "CompletionOutcome",
    "ConnectedSocket",
    "ConnectionClosedError",
    "ControlEvent",
    "ControlRequest",
    "EmptyPayload",
    "EventKind",
    "EventPayload",
    "FailedPayload",
    "FrameDecoder",
    "FrameTooLargeError",
    "FramedTransport",
    "IncompatiblePayload",
    "MalformedFrameError",
    "ProgressPayload",
    "ProgressPhase",
    "ProtocolErrorCode",
    "ProtocolFailureError",
    "ProviderPayload",
    "RequestKind",
    "RequestPayload",
    "ServiceStopReason",
    "ServiceStoppingPayload",
    "SnapshotPayload",
    "decode_event",
    "decode_request",
    "encode_event",
    "encode_frame",
    "encode_request",
    "new_request_id",
]
