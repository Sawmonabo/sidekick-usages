"""Closed and structural types for the local supervisor protocol."""

from enum import StrEnum
from typing import Protocol

MAX_PROTOCOL_VERSION = 65_535
PROTOCOL_VERSION = 3


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
    PARTICIPANT_REGISTER = "participant_register"
    PARTICIPANT_SUBSCRIBE = "participant_subscribe"
    TURN_BEGIN = "turn_begin"
    TURN_END = "turn_end"
    PARTICIPANT_READY = "participant_ready"
    PARTICIPANT_ADOPT = "participant_adopt"
    SELECT_ACCOUNT = "select_account"
    SELECTION_STATUS = "selection_status"


class EventKind(StrEnum):
    """Closed supervisor event vocabulary."""

    ACCEPTED = "accepted"
    SNAPSHOT = "snapshot"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"
    SERVICE_STOPPING = "service_stopping"
    PARTICIPANT_REGISTERED = "participant_registered"
    TURN_ADMISSION = "turn_admission"
    PARTICIPANT_NOTICE = "participant_notice"
    SELECTION_RESULT = "selection_result"
    SELECTION_STATUS = "selection_status"


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


class ControlOperationIdentity(StrEnum):
    """Expected operation identity for one control action stream."""

    ACCOUNT = "account"
    PROVIDER = "provider"
    GLOBAL = "global"


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


class ConnectedSocket(Protocol):
    """Minimal connected byte-stream socket used by the protocol."""

    def recv(self, size: int, /) -> bytes:
        """Receive at most ``size`` bytes."""

    def sendall(self, data: bytes, /) -> None:
        """Send all bytes or raise an operating-system error."""

    def settimeout(self, value: float | None, /) -> None:
        """Set the blocking operation timeout."""

    def shutdown(self, how: int, /) -> None:
        """Disable communication in the requested directions."""

    def close(self) -> None:
        """Close this connection."""
