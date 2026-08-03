"""Bounded secret-free supervisor control protocol."""

import os
import socket
from array import array
from collections import deque
from contextlib import suppress
from threading import Event

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
from sidekick_usages.daemon.selection.protocol import (
    decode_selection_event,
    decode_selection_request,
    encode_selection_event,
    encode_selection_request,
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
        self._pending_descriptors: deque[int | None] = deque()
        self._received_descriptor: int | None = None
        self._closed = Event()

    def receive_payload(self) -> bytes:
        """Block until one complete payload is available."""
        payload, descriptor = self.receive_payload_with_descriptor()
        if descriptor is not None:
            os.close(descriptor)
            raise MalformedFrameError
        return payload

    def receive_payload_with_descriptor(self) -> tuple[bytes, int | None]:
        """Receive one payload and its exact optional SCM_RIGHTS descriptor."""
        if self._pending:
            return self._take_pending()
        while True:
            chunk, descriptor = self._read_chunk()
            if not chunk:
                self._raise_closed(descriptor)
            self._retain_descriptor(descriptor)
            self._queue_frames(self._decoder.feed(chunk))
            if self._pending:
                return self._take_pending()

    def _take_pending(self) -> tuple[bytes, int | None]:
        return (
            self._pending.popleft(),
            self._pending_descriptors.popleft(),
        )

    def _read_chunk(self) -> tuple[bytes, int | None]:
        try:
            return self._receive_chunk()
        except OSError:
            if not self._closed.is_set():
                raise
            return b"", None

    def _raise_closed(self, descriptor: int | None) -> None:
        if descriptor is not None:
            os.close(descriptor)
        if self._received_descriptor is not None:
            os.close(self._received_descriptor)
            self._received_descriptor = None
        self._decoder.finish()
        raise ConnectionClosedError("The local control connection closed.")

    def _retain_descriptor(self, descriptor: int | None) -> None:
        if descriptor is None:
            return
        if self._received_descriptor is not None:
            os.close(descriptor)
            os.close(self._received_descriptor)
            self._received_descriptor = None
            raise MalformedFrameError
        self._received_descriptor = descriptor

    def _queue_frames(self, frames: tuple[bytes, ...]) -> None:
        for index, frame in enumerate(frames):
            self._pending.append(frame)
            self._pending_descriptors.append(
                self._received_descriptor if index == 0 else None
            )
            if index == 0:
                self._received_descriptor = None

    def receive_request(self) -> ControlRequest:
        """Receive and strictly decode one client request."""
        return decode_request(self.receive_payload())

    def receive_request_with_attachment(
        self,
    ) -> tuple[ControlRequest, socket.socket | None]:
        """Decode one request and take ownership of its optional endpoint."""
        payload, descriptor = self.receive_payload_with_descriptor()
        try:
            request = decode_request(payload)
            if descriptor is None:
                return request, None
            os.set_inheritable(descriptor, False)
            return request, socket.socket(fileno=descriptor)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise

    def receive_event(self) -> ControlEvent:
        """Receive and strictly decode one supervisor event."""
        return decode_event(self.receive_payload())

    def send_request(self, request: ControlRequest) -> None:
        """Encode and send one request frame."""
        self._connection.sendall(encode_request(request))

    def send_request_with_attachment(
        self,
        request: ControlRequest,
        endpoint: socket.socket,
    ) -> None:
        """Send one frame carrying exactly one duplicated local descriptor."""
        if not isinstance(self._connection, socket.socket):
            raise OSError("The control connection cannot transfer endpoints.")
        frame = encode_request(request)
        rights = array("i", (endpoint.fileno(),))
        sent = self._connection.sendmsg(
            (frame,),
            ((socket.SOL_SOCKET, socket.SCM_RIGHTS, rights),),
        )
        if sent <= 0:
            raise ConnectionClosedError(
                "The local control connection closed."
            )
        if sent < len(frame):
            self._connection.sendall(frame[sent:])

    def send_event(self, event: ControlEvent) -> None:
        """Encode and send one event frame."""
        self._connection.sendall(encode_event(event))

    def close(self) -> None:
        """Wake blocked I/O and close the connected stream."""
        self._closed.set()
        if self._received_descriptor is not None:
            os.close(self._received_descriptor)
            self._received_descriptor = None
        while self._pending_descriptors:
            descriptor = self._pending_descriptors.popleft()
            if descriptor is not None:
                os.close(descriptor)
        with suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        self._connection.close()

    def _receive_chunk(self) -> tuple[bytes, int | None]:
        if not isinstance(self._connection, socket.socket):
            return self._connection.recv(READ_CHUNK_BYTES), None
        item_size = array("i").itemsize
        receive_flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
        chunk, ancillary, flags, _address = self._connection.recvmsg(
            READ_CHUNK_BYTES,
            socket.CMSG_SPACE(item_size),
            receive_flags,
        )
        descriptors: list[int] = []
        invalid_ancillary = False
        for level, kind, payload in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                invalid_ancillary = True
                continue
            if len(payload) < item_size:
                invalid_ancillary = True
                continue
            values = array("i")
            aligned_size = len(payload) - len(payload) % item_size
            values.frombytes(payload[:aligned_size])
            descriptors.extend(values)
        if (
            flags & socket.MSG_CTRUNC
            or invalid_ancillary
            or len(descriptors) > 1
        ):
            for descriptor in descriptors:
                os.close(descriptor)
            raise MalformedFrameError
        if descriptors and os.get_inheritable(descriptors[0]):
            os.set_inheritable(descriptors[0], False)
        return chunk, None if not descriptors else descriptors[0]


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
            "provider": payload.provider_id.value,
        }
    if isinstance(payload, AccountPayload):
        return {
            "account_id": str(payload.account_id),
            "provider": payload.provider_id.value,
        }
    if isinstance(payload, ProviderPayload):
        return {"provider": payload.provider_id.value}
    return encode_selection_request(payload)


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
    elif isinstance(payload, ServiceStoppingPayload):
        result = {"reason": payload.reason.value}
    else:
        return encode_selection_event(payload)
    return result


def _decode_request_payload(
    kind: RequestKind,
    value: JsonValue,
) -> RequestPayload:
    root = _require_object(value)
    if kind is RequestKind.ACTIVATE:
        _require_exact_keys(root, {"account_id", "provider"})
        return ActivationPayload(
            provider_id=ProviderId(_require_string(root["provider"])),
            account_id=SidekickAccountId(_require_string(root["account_id"])),
        )
    if kind in {RequestKind.REFRESH_ACCOUNT, RequestKind.SELECT_ACCOUNT}:
        _require_exact_keys(root, {"account_id", "provider"})
        return AccountPayload(
            provider_id=ProviderId(_require_string(root["provider"])),
            account_id=SidekickAccountId(_require_string(root["account_id"])),
        )
    if kind in {RequestKind.RECONCILE, RequestKind.SELECTION_STATUS}:
        _require_exact_keys(root, {"provider"})
        return ProviderPayload(
            provider_id=ProviderId(_require_string(root["provider"]))
        )
    if kind in {
        RequestKind.PARTICIPANT_REGISTER,
        RequestKind.PARTICIPANT_SUBSCRIBE,
        RequestKind.TURN_BEGIN,
        RequestKind.TURN_END,
        RequestKind.PARTICIPANT_READY,
        RequestKind.PARTICIPANT_ADOPT,
    }:
        return decode_selection_request(kind, value)
    _require_exact_keys(root, set())
    return EmptyPayload()


def _decode_event_payload(
    kind: EventKind,
    value: JsonValue,
) -> EventPayload:
    if kind in {
        EventKind.PARTICIPANT_REGISTERED,
        EventKind.TURN_ADMISSION,
        EventKind.PARTICIPANT_NOTICE,
        EventKind.SELECTION_RESULT,
        EventKind.SELECTION_STATUS,
    }:
        return decode_selection_event(kind, value)
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
