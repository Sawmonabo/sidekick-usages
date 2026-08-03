"""Strict secret-free request and event models for local control."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    SelectionResult,
    safe_outcome_code,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantReadyRequest,
    ParticipantRegistration,
    SelectionStatus,
    TurnAdmission,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.types.protocol import (
    MAX_PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
    ProgressPhase,
    ProtocolErrorCode,
    RequestKind,
    ServiceStopReason,
)
from sidekick_usages.daemon.types.service import PackageVersion

_MAX_INTEGER = (1 << 63) - 1

type RequestPayload = (
    EmptyPayload
    | ActivationPayload
    | AccountPayload
    | ProviderPayload
    | ParticipantManifest
    | ParticipantConnectionRequest
    | TurnBeginRequest
    | TurnEndRequest
    | ParticipantReadyRequest
    | ParticipantAdoptionRequest
)
type ControlActionTerminalPayload = (
    CompletedPayload
    | FailedPayload
    | IncompatiblePayload
    | ServiceStoppingPayload
    | ParticipantRegistration
    | TurnAdmission
    | ParticipantNotice
    | SelectionResult
    | SelectionStatus
)
type EventPayload = (
    AcceptedPayload
    | SnapshotPayload
    | ProgressPayload
    | CompletedPayload
    | FailedPayload
    | IncompatiblePayload
    | ServiceStoppingPayload
    | ParticipantRegistration
    | TurnAdmission
    | ParticipantNotice
    | SelectionResult
    | SelectionStatus
)


@dataclass(frozen=True, slots=True)
class EmptyPayload:
    """Payload for requests that accept no arguments."""


@dataclass(frozen=True, slots=True)
class ActivationPayload:
    """One stable account target and its explicit activation approval."""

    provider_id: ProviderId
    account_id: SidekickAccountId
    allow_remote_control_disconnect: bool = False

    def __post_init__(self) -> None:
        """Restrict Remote Control approval to Claude activation."""
        if type(self.allow_remote_control_disconnect) is not bool:
            raise ValueError("Remote Control approval must be boolean.")
        if (
            self.allow_remote_control_disconnect
            and self.provider_id is not ProviderId.CLAUDE
        ):
            raise ValueError(
                "Remote Control approval is only valid for Claude activation."
            )


@dataclass(frozen=True, slots=True)
class AccountPayload:
    """One stable account target without a label or credential."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    """One provider target without provider-owned identity."""

    provider_id: ProviderId


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
    code: str

    def __post_init__(self) -> None:
        """Require one bounded machine-readable failure code."""
        if safe_outcome_code(self.code) is None:
            raise ValueError("Failed events require a safe code.")


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
        PackageVersion(self.package_version)
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
        PackageVersion(self.package_version)
        _require_event_payload(self.kind, self.payload)


def _require_nonnegative_integer(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_INTEGER
    ):
        raise ValueError("Revision must be a nonnegative bounded integer.")


def _require_protocol_version(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PROTOCOL_VERSION
    ):
        raise ValueError("Protocol version must be a bounded integer.")


def _require_request_payload(
    kind: RequestKind,
    payload: RequestPayload,
) -> None:
    expected: dict[RequestKind, type[RequestPayload]] = {
        RequestKind.ACTIVATE: ActivationPayload,
        RequestKind.REFRESH_ACCOUNT: AccountPayload,
        RequestKind.RECONCILE: ProviderPayload,
        RequestKind.PARTICIPANT_REGISTER: ParticipantManifest,
        RequestKind.PARTICIPANT_SUBSCRIBE: ParticipantConnectionRequest,
        RequestKind.TURN_BEGIN: TurnBeginRequest,
        RequestKind.TURN_END: TurnEndRequest,
        RequestKind.PARTICIPANT_READY: ParticipantReadyRequest,
        RequestKind.PARTICIPANT_ADOPT: ParticipantAdoptionRequest,
        RequestKind.SELECT_ACCOUNT: AccountPayload,
        RequestKind.SELECTION_STATUS: ProviderPayload,
    }
    if isinstance(payload, expected.get(kind, EmptyPayload)):
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
        EventKind.PARTICIPANT_REGISTERED: ParticipantRegistration,
        EventKind.TURN_ADMISSION: TurnAdmission,
        EventKind.PARTICIPANT_NOTICE: ParticipantNotice,
        EventKind.SELECTION_RESULT: SelectionResult,
        EventKind.SELECTION_STATUS: SelectionStatus,
    }
    if not isinstance(payload, expected[kind]):
        raise ValueError("Event kind and payload do not match.")
