"""Secret-free live participant and turn-admission models."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId

_MAX_GENERATION = 2**63 - 1
SUPPORTED_PARTICIPANT_CAPABILITY_VERSION = 1


class ParticipantRequestError(RuntimeError):
    """Typed participant refusal safe for the control protocol."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class SelectionRequestError(RuntimeError):
    """Typed operator selection refusal safe for the control protocol."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class TurnAdmissionState(StrEnum):
    """Closed result of one participant turn-begin request."""

    ADMITTED = "admitted"
    QUEUED = "queued"


class ParticipantClientKind(StrEnum):
    """Closed integrated clients that can join selection coordination."""

    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"


class ParticipantNoticeKind(StrEnum):
    """Closed admission notices delivered to participant subscribers."""

    PREPARE = "prepare"
    OPEN = "open"
    STATUS = "status"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantManifest:
    """Bounded secret-free identity claimed by one integrated client."""

    participant_id: ParticipantId
    provider_id: ProviderId
    client_kind: ParticipantClientKind
    capability_version: int
    connection_generation: int

    def __post_init__(self) -> None:
        """Require safe client metadata and a positive connection epoch."""
        expected_provider = {
            ParticipantClientKind.CLAUDE_CODE: ProviderId.CLAUDE,
            ParticipantClientKind.CODEX_CLI: ProviderId.CODEX,
        }.get(self.client_kind)
        if expected_provider is not self.provider_id:
            raise ValueError("Participant client kind and provider differ.")
        if (
            type(self.capability_version) is not int
            or self.capability_version
            != SUPPORTED_PARTICIPANT_CAPABILITY_VERSION
        ):
            raise ValueError("Participant capability version is invalid.")
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantRegistration:
    """Authenticated participant state at one admission boundary."""

    participant_id: ParticipantId
    provider_id: ProviderId
    connection_generation: int
    registered_epoch: SelectionEpoch
    pending_epoch: SelectionEpoch | None

    def __post_init__(self) -> None:
        """Require a pending epoch to advance the registered epoch."""
        _require_generation(self.connection_generation)
        if (
            self.pending_epoch is not None
            and self.pending_epoch.value <= self.registered_epoch.value
        ):
            raise ValueError("Participant pending epoch is not newer.")


@dataclass(frozen=True, slots=True)
class ParticipantConnectionRequest:
    """Authenticate one participant control action or subscription."""

    participant_id: ParticipantId
    connection_generation: int

    def __post_init__(self) -> None:
        """Require one positive authenticated connection generation."""
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True)
class TurnBeginRequest:
    """One secret-free participant request to begin an exact turn."""

    participant_id: ParticipantId
    connection_generation: int
    turn_id: TurnId

    def __post_init__(self) -> None:
        """Require one positive authenticated connection generation."""
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True)
class TurnEndRequest:
    """One secret-free completion for an admitted turn lease."""

    participant_id: ParticipantId
    connection_generation: int
    turn_id: TurnId

    def __post_init__(self) -> None:
        """Require one positive authenticated connection generation."""
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnAdmission:
    """Epoch binding or in-memory queue result for one turn."""

    participant_id: ParticipantId
    turn_id: TurnId
    state: TurnAdmissionState
    epoch: SelectionEpoch | None
    account_id: SidekickAccountId | None
    generation: AuthorityGeneration | None

    def __post_init__(self) -> None:
        """Require complete authority only for admitted turns."""
        authority = (self.epoch, self.account_id, self.generation)
        if self.state is TurnAdmissionState.ADMITTED:
            if any(value is None for value in authority):
                raise ValueError("Admitted turns require exact authority.")
        elif any(value is not None for value in authority):
            raise ValueError("Queued turns cannot claim authority.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantReadyProof:
    """Proof that one participant can bind its next turn to an epoch."""

    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantReadyRequest:
    """One participant readiness proof with connection authority."""

    participant_id: ParticipantId
    connection_generation: int
    proof: ParticipantReadyProof

    def __post_init__(self) -> None:
        """Require one positive authenticated connection generation."""
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantAdoptionProof:
    """Ephemeral first-real-turn routing proof."""

    turn_id: TurnId
    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantAdoptionRequest:
    """One first-real-turn proof with connection authority."""

    participant_id: ParticipantId
    connection_generation: int
    proof: ParticipantAdoptionProof

    def __post_init__(self) -> None:
        """Require one positive authenticated connection generation."""
        _require_generation(self.connection_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantNotice:
    """One secret-free participant admission notice."""

    participant_id: ParticipantId
    provider_id: ProviderId
    kind: ParticipantNoticeKind
    epoch: SelectionEpoch
    code: SelectionCode | None = None

    def __post_init__(self) -> None:
        """Require status codes only on typed status notices."""
        if (self.kind is ParticipantNoticeKind.STATUS) != (
            self.code is not None
        ):
            raise ValueError("Participant notice kind and code disagree.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectionStatus:
    """One provider selection snapshot for operator status."""

    provider_id: ProviderId
    operation_id: OperationId | None
    finalized_account_id: SidekickAccountId | None
    finalized_epoch: SelectionEpoch | None
    target_account_id: SidekickAccountId | None
    pending_epoch: SelectionEpoch | None
    phase: SelectionPhase | None
    code: SelectionCode | None
    registered_count: int = 0
    reachable_count: int = 0
    required_count: int = 0
    ready_count: int = 0
    adopted_count: int = 0
    unreachable_count: int = 0
    active_turn_count: int = 0
    queued_turn_count: int = 0

    def __post_init__(self) -> None:
        """Require complete finalized and active selection facts."""
        finalized = (self.finalized_account_id, self.finalized_epoch)
        active = (
            self.operation_id,
            self.target_account_id,
            self.pending_epoch,
            self.phase,
        )
        if any(value is None for value in finalized) and any(
            value is not None for value in finalized
        ):
            raise ValueError("Finalized selection status is incomplete.")
        if any(value is None for value in active) and any(
            value is not None for value in active
        ):
            raise ValueError("Active selection status is incomplete.")
        counts = (
            self.registered_count,
            self.reachable_count,
            self.required_count,
            self.ready_count,
            self.adopted_count,
            self.unreachable_count,
            self.active_turn_count,
            self.queued_turn_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Selection status counts are invalid.")
        if (
            self.reachable_count + self.unreachable_count
            > self.registered_count
            or self.ready_count > self.required_count
            or self.adopted_count > self.registered_count
        ):
            raise ValueError("Selection status counts are incoherent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantSnapshot:
    """Bounded provider participant state without process identity."""

    provider_id: ProviderId
    required_participant_ids: tuple[ParticipantId, ...]
    ready_participant_ids: tuple[ParticipantId, ...]
    confirmed_dead_participant_ids: tuple[ParticipantId, ...]
    unreachable_participant_ids: tuple[ParticipantId, ...]
    active_turn_count: int
    registered_count: int = 0
    reachable_count: int = 0
    adopted_count: int = 0
    queued_turn_count: int = 0


def _require_generation(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_GENERATION:
        raise ValueError("Connection generation is invalid.")
