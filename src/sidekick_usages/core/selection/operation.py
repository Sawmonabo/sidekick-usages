"""Validated models for one globally coordinated account selection."""

from dataclasses import dataclass
from datetime import datetime
from typing import Self

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId

_MAX_SELECTION_EPOCH = 2**63 - 1
_MAX_SELECTION_PARTICIPANTS = 512


@dataclass(frozen=True, slots=True, order=True)
class SelectionEpoch:
    """Monotonic non-secret generation of selected provider authority."""

    value: int

    def __post_init__(self) -> None:
        """Require one signed 64-bit non-negative epoch."""
        if (
            type(self.value) is not int
            or self.value < 0
            or self.value > _MAX_SELECTION_EPOCH
        ):
            raise ValueError("Selection epoch is outside the supported bound.")

    def next(self) -> Self:
        """Return the next epoch or fail closed at the upper bound."""
        if self.value == _MAX_SELECTION_EPOCH:
            raise ValueError("Selection epoch cannot advance past its bound.")
        return type(self)(self.value + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalizedSelection:
    """Last globally finalized saved account for one provider."""

    provider_id: ProviderId
    account_id: SidekickAccountId
    epoch: SelectionEpoch
    generation: AuthorityGeneration
    finalized_at: datetime

    def __post_init__(self) -> None:
        """Normalize the trusted finalization timestamp to UTC."""
        object.__setattr__(
            self,
            "finalized_at",
            as_utc(self.finalized_at),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedSelection:
    """Provider-validated target bound to coordinator-owned epochs."""

    operation_id: OperationId
    provider_id: ProviderId
    target_account_id: SidekickAccountId
    target_generation: AuthorityGeneration
    baseline_epoch: SelectionEpoch
    pending_epoch: SelectionEpoch

    def __post_init__(self) -> None:
        """Require exactly one forward epoch transition."""
        if self.pending_epoch != self.baseline_epoch.next():
            raise ValueError("Prepared selection must advance one epoch.")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityReadyProof:
    """Sanitized provider proof for one committed selected authority."""

    provider_id: ProviderId
    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch
    safe_code: SelectionCode


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenSelectionOperation:
    """One bounded secret-free global selection still in progress."""

    operation_id: OperationId
    provider_id: ProviderId
    baseline_account_id: SidekickAccountId | None
    target_account_id: SidekickAccountId
    target_generation: AuthorityGeneration | None
    baseline_epoch: SelectionEpoch
    pending_epoch: SelectionEpoch
    phase: SelectionPhase
    required_participant_ids: tuple[ParticipantId, ...]
    ready_participant_ids: tuple[ParticipantId, ...]
    lost_after_commit_participant_ids: tuple[ParticipantId, ...]
    confirmed_dead_before_commit_count: int
    confirmed_dead_before_commit_code: SelectionCode | None
    outcome_code: SelectionCode | None
    started_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Normalize time and participant sets for durable comparison."""
        if self.pending_epoch != self.baseline_epoch.next():
            raise ValueError("Open selection must advance one epoch.")
        required = _selection_participant_ids(
            self.required_participant_ids,
            name="Required participants",
        )
        ready = _selection_participant_ids(
            self.ready_participant_ids,
            name="Ready participants",
        )
        lost = _selection_participant_ids(
            self.lost_after_commit_participant_ids,
            name="Lost participants",
        )
        _require_selection_participant_relation(required, ready, lost)
        _require_confirmed_dead_state(
            self.confirmed_dead_before_commit_count,
            self.confirmed_dead_before_commit_code,
        )
        _require_open_selection_phase(self, required, ready, lost)
        _require_open_selection_outcome(self.phase, lost, self.outcome_code)
        started_at = as_utc(self.started_at)
        updated_at = as_utc(self.updated_at)
        if updated_at < started_at:
            raise ValueError("Selection update cannot predate its start.")
        object.__setattr__(self, "required_participant_ids", required)
        object.__setattr__(self, "ready_participant_ids", ready)
        object.__setattr__(
            self,
            "lost_after_commit_participant_ids",
            lost,
        )
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectionResult:
    """Bounded terminal selection result without participant identities."""

    operation_id: OperationId
    provider_id: ProviderId
    target_account_id: SidekickAccountId
    target_generation: AuthorityGeneration | None
    epoch: SelectionEpoch
    outcome: SelectionOutcome
    safe_code: SelectionCode
    required_count: int
    ready_count: int
    adopted_count: int
    lost_count: int
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        """Normalize time and enforce bounded coherent result counts."""
        counts = (
            self.required_count,
            self.ready_count,
            self.adopted_count,
            self.lost_count,
        )
        if any(
            type(count) is not int
            or count < 0
            or count > _MAX_SELECTION_PARTICIPANTS
            for count in counts
        ):
            raise ValueError("Selection participant count is invalid.")
        if self.adopted_count != 0:
            raise ValueError(
                "Selection adoption is not durable journal state."
            )
        if self.ready_count + self.lost_count > self.required_count:
            raise ValueError("Selection participant counts are incoherent.")
        _require_selection_result_outcome(self)
        started_at = as_utc(self.started_at)
        completed_at = as_utc(self.completed_at)
        if completed_at < started_at:
            raise ValueError("Selection completion cannot predate its start.")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)


def _require_open_selection_outcome(
    phase: SelectionPhase,
    lost: tuple[ParticipantId, ...],
    outcome_code: SelectionCode | None,
) -> None:
    """Require one phase-owned open-operation outcome marker."""
    if phase in {
        SelectionPhase.PREVALIDATING,
        SelectionPhase.PREPARING,
        SelectionPhase.WAITING_OLD_TURNS,
        SelectionPhase.COMMITTING,
    }:
        if lost or outcome_code is not None:
            raise ValueError(
                "Pre-readiness selection cannot claim an outcome."
            )
        return
    if phase is SelectionPhase.AWAITING_READY:
        expected = (
            None if not lost else SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
        )
        if outcome_code is not expected:
            raise ValueError("Awaiting selection outcome is incoherent.")
        return
    if outcome_code is not SelectionCode.SELECTION_RECOVERY_REQUIRED:
        raise ValueError(
            "Recovering selection requires its safe outcome code."
        )


def _require_selection_result_outcome(result: SelectionResult) -> None:
    """Require exact generation, count, outcome, and code coherence."""
    expected_code = {
        SelectionOutcome.READY: SelectionCode.SELECTION_SUCCEEDED,
        SelectionOutcome.FAILED_OLD_EPOCH: (
            SelectionCode.SELECTION_ROLLED_BACK
        ),
        SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT: (
            SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
        ),
        SelectionOutcome.RECOVERY_REQUIRED: (
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        ),
    }[result.outcome]
    if result.safe_code is not expected_code:
        raise ValueError("Selection outcome and safe code disagree.")
    if result.outcome is SelectionOutcome.FAILED_OLD_EPOCH:
        if result.ready_count or result.lost_count:
            raise ValueError("Old-epoch failure cannot claim readiness.")
        return
    if result.target_generation is None:
        raise ValueError("Postvalidation result requires target generation.")
    if result.outcome is SelectionOutcome.READY:
        if result.lost_count or result.ready_count != result.required_count:
            raise ValueError("Ready selection requires every participant.")
        return
    if result.outcome is SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT and (
        result.lost_count == 0
        or result.ready_count + result.lost_count != result.required_count
    ):
        raise ValueError("Degraded selection requires resolved participants.")


def _require_selection_participant_relation(
    required: tuple[ParticipantId, ...],
    ready: tuple[ParticipantId, ...],
    lost: tuple[ParticipantId, ...],
) -> None:
    """Require coherent required, ready, and lost participant sets."""
    if not set(ready).issubset(required):
        raise ValueError("Ready participants must be required.")
    if not set(lost).issubset(required):
        raise ValueError("Lost participants must remain required.")
    if set(ready) & set(lost):
        raise ValueError("Ready and lost participants must be disjoint.")


def _require_confirmed_dead_state(
    count: int,
    code: SelectionCode | None,
) -> None:
    """Require one bounded precommit confirmed-dead marker."""
    if (
        type(count) is not int
        or count < 0
        or count > _MAX_SELECTION_PARTICIPANTS
    ):
        raise ValueError("Confirmed-dead participant count is invalid.")
    expected = None if count == 0 else SelectionCode.PARTICIPANT_CONFIRMED_DEAD
    if code is not expected:
        raise ValueError("Confirmed-dead participant code is invalid.")


def _require_open_selection_phase(
    operation: OpenSelectionOperation,
    required: tuple[ParticipantId, ...],
    ready: tuple[ParticipantId, ...],
    lost: tuple[ParticipantId, ...],
) -> None:
    """Require generation and participant facts owned by the phase."""
    if operation.phase is SelectionPhase.PREVALIDATING:
        if (
            operation.target_generation is not None
            or required
            or ready
            or lost
            or operation.confirmed_dead_before_commit_count
            or operation.outcome_code is not None
        ):
            raise ValueError(
                "Prevalidation cannot claim target or participant proof."
            )
    elif operation.target_generation is None:
        raise ValueError("Prepared selection requires target generation.")


def _selection_participant_ids(
    participant_ids: tuple[ParticipantId, ...],
    *,
    name: str,
) -> tuple[ParticipantId, ...]:
    """Validate and sort one bounded participant identity set."""
    if not isinstance(participant_ids, tuple) or any(
        not isinstance(participant_id, ParticipantId)
        for participant_id in participant_ids
    ):
        raise TypeError(f"{name} must be participant IDs.")
    if len(participant_ids) > _MAX_SELECTION_PARTICIPANTS or len(
        set(participant_ids)
    ) != len(participant_ids):
        raise ValueError(f"{name} must be bounded and unique.")
    return tuple(sorted(participant_ids))
