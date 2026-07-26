"""Bounded secret-free supervisor control protocol."""

import socket
from collections import deque
from contextlib import suppress

from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    ActivationPayload,
    CompletedPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    EventPayload,
    FailedPayload,
    IncompatiblePayload,
    ProgressPayload,
    ProviderPayload,
    RequestPayload,
    ServiceStoppingPayload,
    SnapshotPayload,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    ConnectedSocket,
    EventKind,
    ProgressPhase,
    ProtocolErrorCode,
    RequestKind,
    ServiceStopReason,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    EmptyFrameError,
    IncompleteFrameError,
    OversizedFrameError,
    encode_bounded_frame,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    JsonEncodeError,
    JsonObject,
    JsonValue,
    decode_integer_json_value,
    encode_compact_json,
)

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 65_536
MAX_REQUESTS_PER_CONNECTION = 128
READ_CHUNK_BYTES = 16_384
UNATTRIBUTED_REQUEST_ID = RequestId("00000000-0000-0000-0000-000000000000")


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


class FrameDecoder:
    """Incrementally decode length-prefixed frames with a bounded buffer."""

    def __init__(self) -> None:
        self._decoder = BoundedFrameDecoder(MAX_FRAME_BYTES)

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        """Consume one fragment and return every newly complete payload."""
        try:
            return tuple(bytes(frame) for frame in self._decoder.feed(chunk))
        except OversizedFrameError:
            raise FrameTooLargeError from None
        except EmptyFrameError:
            raise MalformedFrameError from None

    def finish(self) -> None:
        """Reject an incomplete prefix or payload at end of stream."""
        try:
            self._decoder.finish()
        except IncompleteFrameError:
            raise MalformedFrameError from None


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
        """Wake blocked I/O and close the connected stream."""
        with suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        self._connection.close()


def encode_frame(payload: bytes) -> bytes:
    """Prefix one nonempty bounded payload with its big-endian length."""
    try:
        return bytes(encode_bounded_frame(payload, MAX_FRAME_BYTES))
    except EmptyFrameError:
        raise MalformedFrameError from None
    except OversizedFrameError:
        raise FrameTooLargeError from None


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


def _request_payload_to_json(payload: RequestPayload) -> JsonValue:
    if isinstance(payload, EmptyPayload):
        return {}
    if isinstance(payload, ActivationPayload):
        return {
            "account_id": str(payload.account_id),
            "allow_remote_control_disconnect": (
                payload.allow_remote_control_disconnect
            ),
            "provider": payload.provider_id.value,
        }
    if isinstance(payload, AccountPayload):
        return {
            "account_id": str(payload.account_id),
            "provider": payload.provider_id.value,
        }
    return {"provider": payload.provider_id.value}


def _event_payload_to_json(payload: EventPayload) -> JsonValue:
    result: dict[str, JsonValue]
    if isinstance(payload, AcceptedPayload):
        result = {"operation_id": _optional_operation_id(payload.operation_id)}
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
            "code": payload.code,
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
    if kind is RequestKind.ACTIVATE:
        _require_exact_keys(
            root,
            {
                "account_id",
                "allow_remote_control_disconnect",
                "provider",
            },
        )
        return ActivationPayload(
            provider_id=ProviderId(_require_string(root["provider"])),
            account_id=SidekickAccountId(_require_string(root["account_id"])),
            allow_remote_control_disconnect=_require_boolean(
                root["allow_remote_control_disconnect"]
            ),
        )
    if kind is RequestKind.REFRESH_ACCOUNT:
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
            code=_require_safe_code(root["code"]),
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
        return encode_compact_json(root)
    except JsonEncodeError:
        raise MalformedFrameError from None


def _decode_json(payload: bytes) -> JsonValue:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameTooLargeError
        raise MalformedFrameError
    try:
        return decode_integer_json_value(payload)
    except JsonDecodeError:
        raise MalformedFrameError from None


def _require_object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise MalformedFrameError
    return value


def _require_exact_keys(
    value: JsonObject,
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


def _require_boolean(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise MalformedFrameError
    return value


def _require_safe_code(value: JsonValue) -> str:
    code = _require_string(value)
    if safe_outcome_code(code) is None:
        raise MalformedFrameError
    return code
