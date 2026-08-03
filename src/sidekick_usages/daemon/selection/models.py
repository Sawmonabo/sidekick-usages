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
_MAX_CAPABILITY_VERSION = 65_535


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
            or not 1 <= self.capability_version <= _MAX_CAPABILITY_VERSION
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantSnapshot:
    """Bounded provider participant state without process identity."""

    provider_id: ProviderId
    required_participant_ids: tuple[ParticipantId, ...]
    ready_participant_ids: tuple[ParticipantId, ...]
    confirmed_dead_participant_ids: tuple[ParticipantId, ...]
    unreachable_participant_ids: tuple[ParticipantId, ...]
    active_turn_count: int


def _require_generation(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_GENERATION:
        raise ValueError("Connection generation is invalid.")
