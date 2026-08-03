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
MAX_ACTIVE_TURNS_PER_PROVIDER = 128
MAX_PARTICIPANTS_PER_PROVIDER = 16
MAX_PENDING_BEGINS_PER_PROVIDER = 128
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
    READY = "ready"
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
        """Require one legal normal or recovery epoch relation."""
        _require_generation(self.connection_generation)
        if self.pending_epoch is not None and self.pending_epoch.value not in {
            self.registered_epoch.value,
            self.registered_epoch.value + 1,
        }:
            raise ValueError(
                "Participant registration epoch relation is invalid."
            )


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
    operation_id: OperationId | None = None
    target_account_id: SidekickAccountId | None = None
    target_generation: AuthorityGeneration | None = None

    def __post_init__(self) -> None:
        """Require fields owned only by status and ready notices."""
        if (self.kind is ParticipantNoticeKind.STATUS) != (
            self.code is not None
        ):
            raise ValueError("Participant notice kind and code disagree.")
        target = (
            self.operation_id,
            self.target_account_id,
            self.target_generation,
        )
        if self.kind is ParticipantNoticeKind.READY:
            if any(value is None for value in target):
                raise ValueError("Ready participant notice is incomplete.")
        elif any(value is not None for value in target):
            raise ValueError("Only ready notices carry target authority.")


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
    confirmed_dead_count: int = 0
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
        active_operation = self.operation_id is not None
        if self.code is not None and not active_operation:
            raise ValueError("Selection status code requires an operation.")
        self._require_epoch_relation(active_operation)
        self._require_phase_state(active_operation)
        self._require_counts()

    def _require_epoch_relation(self, active_operation: bool) -> None:
        """Require exact normal or target-selected recovery epochs."""
        if not active_operation:
            return
        if self.pending_epoch is None or self.phase is None:
            raise ValueError("Active selection status is incomplete.")
        if self.finalized_epoch is None:
            if self.pending_epoch != SelectionEpoch(1):
                raise ValueError("Selection status epoch relation is invalid.")
            return
        normal = self.pending_epoch.value == self.finalized_epoch.value + 1
        recovery = (
            self.pending_epoch == self.finalized_epoch
            and self.target_account_id == self.finalized_account_id
            and self.phase
            in {SelectionPhase.AWAITING_READY, SelectionPhase.RECOVERING}
        )
        if not normal and not recovery:
            raise ValueError("Selection status epoch relation is invalid.")

    def _require_phase_state(self, active_operation: bool) -> None:
        """Require code and gate facts owned by the exact active phase."""
        if not active_operation:
            if (
                self.required_count
                or self.ready_count
                or self.confirmed_dead_count
                or self.queued_turn_count
            ):
                raise ValueError(
                    "Selection status active gate facts require an operation."
                )
            return
        if self.phase is None:
            raise ValueError("Active selection status is incomplete.")
        allowed_codes: frozenset[SelectionCode | None]
        if self.phase is SelectionPhase.AWAITING_READY:
            allowed_codes = frozenset(
                {None, SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT}
            )
        elif self.phase is SelectionPhase.RECOVERING:
            allowed_codes = frozenset(
                {SelectionCode.SELECTION_RECOVERY_REQUIRED}
            )
        else:
            allowed_codes = frozenset({None})
        if self.code not in allowed_codes:
            raise ValueError("Selection status phase and code disagree.")
        if self.phase is SelectionPhase.PREVALIDATING and (
            self.required_count or self.ready_count or self.queued_turn_count
        ):
            raise ValueError("Prevalidation cannot claim active gate facts.")
        if (
            self.phase
            not in {
                SelectionPhase.AWAITING_READY,
                SelectionPhase.RECOVERING,
            }
            and self.ready_count
        ):
            raise ValueError("Precommit selection cannot claim readiness.")
        if (
            self.code is SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
            and not self.confirmed_dead_count
        ):
            raise ValueError("Lost-participant status requires a lost member.")

    def _require_counts(self) -> None:
        """Require bounded counts with coherent semantic ownership."""
        counts = (
            self.registered_count,
            self.reachable_count,
            self.required_count,
            self.ready_count,
            self.confirmed_dead_count,
            self.adopted_count,
            self.unreachable_count,
            self.active_turn_count,
            self.queued_turn_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Selection status counts are invalid.")
        participant_counts = counts[:7]
        if (
            any(
                value > MAX_PARTICIPANTS_PER_PROVIDER
                for value in participant_counts
            )
            or self.active_turn_count > MAX_ACTIVE_TURNS_PER_PROVIDER
            or (self.queued_turn_count > MAX_PENDING_BEGINS_PER_PROVIDER)
        ):
            raise ValueError("Selection status count exceeds its bound.")
        if (
            self.reachable_count + self.unreachable_count
            > self.registered_count
            or self.required_count > self.registered_count
            or self.ready_count > self.required_count
            or self.confirmed_dead_count > self.required_count
            or self.ready_count + self.confirmed_dead_count
            > self.required_count
            or self.ready_count > self.reachable_count
            or self.adopted_count > self.registered_count
        ):
            raise ValueError("Selection status counts are incoherent.")
        if self.finalized_epoch is None and (
            self.adopted_count or self.active_turn_count
        ):
            raise ValueError(
                "Unselected status cannot claim turns or adoption."
            )


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
