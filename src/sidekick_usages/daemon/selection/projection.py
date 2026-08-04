"""Pure validation and projection for participant registry state."""

from dataclasses import dataclass, field, replace
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    MAX_PARTICIPANTS_PER_PROVIDER,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantRequestError,
    ParticipantSnapshot,
    TurnAdmission,
    TurnBeginRequest,
)
from sidekick_usages.platform.models import ProcessIdentity


@dataclass(slots=True)
class ParticipantRecord:
    """Participant data mutated only by the condition-owning registry."""

    manifest: ParticipantManifest
    process_identity: ProcessIdentity
    registered_epoch: SelectionEpoch
    connected: bool = False
    prebootstrap_reachable: bool = False
    confirmed_dead: bool = False
    attachment_ready_epoch: SelectionEpoch | None = None
    ready_epoch: SelectionEpoch | None = None
    adopted_epoch: SelectionEpoch | None = None


@dataclass(slots=True)
class ProviderGate:
    """Passive admission data owned by the condition-owning registry."""

    operation_id: OperationId
    pending_epoch: SelectionEpoch
    required: set[ParticipantId]
    account_id: SidekickAccountId | None = None
    generation: AuthorityGeneration | None = None
    queued: dict[TurnId, TurnBeginRequest] = field(default_factory=dict)
    membership_sealed: bool = False
    sealed: bool = False
    status_code: SelectionCode | None = None


def new_gate(
    operation_id: OperationId,
    pending_epoch: SelectionEpoch,
    required: set[ParticipantId],
) -> ProviderGate:
    """Construct one operation-bound participant admission gate."""
    return ProviderGate(operation_id, pending_epoch, required)


def require_gate_binding(
    gate: ProviderGate,
    operation_id: OperationId,
    pending_epoch: SelectionEpoch,
) -> None:
    """Require one restored gate to belong to the exact operation."""
    if (gate.operation_id, gate.pending_epoch) != (
        operation_id,
        pending_epoch,
    ):
        raise ParticipantRequestError(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )


def require_selected(
    finalized: FinalizedSelection | None,
) -> FinalizedSelection:
    """Return one finalized authority or raise a typed refusal."""
    if finalized is None:
        raise ParticipantRequestError(
            SelectionCode.SESSION_CONFIGURATION_REQUIRED
        )
    return finalized


def project_ready_notices(
    operation_id: OperationId,
    proof: AuthorityReadyProof,
    gate: ProviderGate,
    participants: dict[ParticipantId, ParticipantRecord],
) -> tuple[ParticipantNotice, ...]:
    """Validate and project target binding for reachable required clients."""
    target = (gate.account_id, gate.generation)
    expected = (proof.account_id, proof.generation)
    if (
        gate.operation_id != operation_id
        or gate.pending_epoch != proof.epoch
        or (any(value is not None for value in target) and target != expected)
    ):
        raise ParticipantRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
    return tuple(
        participant_notice(
            participant_id,
            participant,
            kind=ParticipantNoticeKind.READY,
            epoch=proof.epoch,
            operation_id=operation_id,
            target_account_id=proof.account_id,
            target_generation=proof.generation,
        )
        for participant_id in sorted(gate.required)
        if (participant := participants.get(participant_id)) is not None
        and participant.connected
        and not participant.confirmed_dead
        and participant.attachment_ready_epoch == proof.epoch
    )


def require_gate_epoch(
    finalized: FinalizedSelection | None,
    pending_epoch: SelectionEpoch,
    *,
    recovery_target: SidekickAccountId | None = None,
) -> None:
    """Require the exact next epoch or selected-target recovery epoch."""
    finalized_value = 0 if finalized is None else finalized.epoch.value
    allowed = {finalized_value + 1}
    if finalized is not None and recovery_target == finalized.account_id:
        allowed.add(finalized_value)
    if pending_epoch.value not in allowed:
        raise ParticipantRequestError(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )


def require_connection(
    participants: dict[ParticipantId, ParticipantRecord],
    participant_id: ParticipantId,
    connection_generation: int,
) -> ParticipantRecord:
    """Return the exact live participant or raise a typed refusal."""
    participant = participants.get(participant_id)
    if (
        participant is None
        or participant.confirmed_dead
        or participant.manifest.connection_generation != connection_generation
    ):
        raise ParticipantRequestError(SelectionCode.PARTICIPANT_UNREACHABLE)
    return participant


def require_gate(
    gates: dict[ProviderId, ProviderGate],
    provider_id: ProviderId,
) -> ProviderGate:
    """Return the active provider gate or raise a typed refusal."""
    gate = gates.get(provider_id)
    if gate is None:
        raise ParticipantRequestError(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )
    return gate


def require_capacity(
    participants: dict[ParticipantId, ParticipantRecord],
    gates: dict[ProviderId, ProviderGate],
    provider_id: ProviderId,
    participant_id: ParticipantId,
) -> None:
    """Bound the union of live and durably required participants."""
    gate = gates.get(provider_id)
    required = set() if gate is None else set(gate.required)
    member_ids = require_membership_bound(
        participants,
        provider_id,
        required,
    )
    if (
        participant_id not in member_ids
        and len(member_ids) >= MAX_PARTICIPANTS_PER_PROVIDER
    ):
        raise ParticipantRequestError(SelectionCode.ACTIVE_OPERATION_TIMEOUT)


def require_membership_bound(
    participants: dict[ParticipantId, ParticipantRecord],
    provider_id: ProviderId,
    required: set[ParticipantId],
) -> set[ParticipantId]:
    """Require bounded union membership before gate publication."""
    member_ids = required | {
        participant_id
        for participant_id, participant in participants.items()
        if participant.manifest.provider_id is provider_id
        and not participant.confirmed_dead
    }
    if len(member_ids) > MAX_PARTICIPANTS_PER_PROVIDER:
        raise ParticipantRequestError(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )
    return member_ids


def require_reconnect(
    current: ParticipantRecord,
    manifest: ParticipantManifest,
    peer: ProcessIdentity,
) -> None:
    """Require the same process with a strictly newer connection."""
    if (
        current.connected
        or current.process_identity != peer
        or current.manifest.provider_id is not manifest.provider_id
        or current.manifest.client_kind != manifest.client_kind
        or current.manifest.capability_version != manifest.capability_version
        or manifest.connection_generation
        <= current.manifest.connection_generation
    ):
        raise ParticipantRequestError(SelectionCode.PARTICIPANT_UNREACHABLE)


def project_snapshot(
    provider_id: ProviderId,
    participants: dict[ParticipantId, ParticipantRecord],
    turns: dict[TurnId, TurnAdmission],
    gate: ProviderGate | None,
    finalized: FinalizedSelection | None,
) -> ParticipantSnapshot:
    """Project truthful current-epoch counts without process identity."""
    required = set() if gate is None else set(gate.required)
    pending_epoch = None if gate is None else gate.pending_epoch
    provider_participants = {
        participant_id: participant
        for participant_id, participant in participants.items()
        if participant.manifest.provider_id is provider_id
    }
    dead = {
        participant_id
        for participant_id in required
        if (participant := provider_participants.get(participant_id))
        is not None
        and participant.confirmed_dead
    }
    registered = required | {
        participant_id
        for participant_id, participant in provider_participants.items()
        if not participant.confirmed_dead
    }
    reachable = {
        participant_id
        for participant_id in registered
        if (participant := provider_participants.get(participant_id))
        is not None
        and (
            participant.connected
            or (
                finalized is None and participant.prebootstrap_reachable
            )
        )
        and not participant.confirmed_dead
    }
    ready = {
        participant_id
        for participant_id in required
        if (participant := provider_participants.get(participant_id))
        is not None
        and participant.ready_epoch == pending_epoch
        and not participant.confirmed_dead
    }
    unreachable = registered - reachable - dead
    finalized_epoch = None if finalized is None else finalized.epoch
    adopted = {
        participant_id
        for participant_id in registered
        if (participant := provider_participants.get(participant_id))
        is not None
        and finalized_epoch is not None
        and participant.adopted_epoch == finalized_epoch
        and not participant.confirmed_dead
    }
    active_turn_count = sum(
        participants[turn.participant_id].manifest.provider_id is provider_id
        for turn in turns.values()
    )
    return ParticipantSnapshot(
        provider_id=provider_id,
        required_participant_ids=tuple(sorted(required)),
        ready_participant_ids=tuple(sorted(ready)),
        confirmed_dead_participant_ids=tuple(sorted(dead)),
        unreachable_participant_ids=tuple(sorted(unreachable)),
        active_turn_count=active_turn_count,
        registered_count=len(registered),
        reachable_count=len(reachable),
        adopted_count=len(adopted),
        queued_turn_count=0 if gate is None else len(gate.queued),
    )


def project_operation_snapshot(
    operation: OpenSelectionOperation,
    snapshot: ParticipantSnapshot,
    updated_at: datetime,
) -> OpenSelectionOperation:
    """Project exact participant truth into one postcommit operation."""
    recovering = operation.phase is SelectionPhase.COMMITTING
    lost = snapshot.confirmed_dead_participant_ids
    return replace(
        operation,
        phase=(SelectionPhase.RECOVERING if recovering else operation.phase),
        required_participant_ids=snapshot.required_participant_ids,
        ready_participant_ids=snapshot.ready_participant_ids,
        lost_after_commit_participant_ids=lost,
        outcome_code=(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
            if recovering
            else None
            if not lost
            else SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
        ),
        updated_at=updated_at,
    )


def project_notice(
    participant_id: ParticipantId,
    participant: ParticipantRecord,
    gate: ProviderGate | None,
    finalized: FinalizedSelection | None,
    *,
    attachment_required: bool = False,
) -> ParticipantNotice:
    """Project the current admission state for one authenticated stream."""
    if gate is None:
        if finalized is None:
            raise ParticipantRequestError(
                SelectionCode.SESSION_CONFIGURATION_REQUIRED
            )
        return participant_notice(
            participant_id,
            participant,
            kind=(
                ParticipantNoticeKind.PREPARE
                if attachment_required
                and participant.attachment_ready_epoch != finalized.epoch
                else ParticipantNoticeKind.OPEN
            ),
            epoch=finalized.epoch,
        )
    target = (gate.account_id, gate.generation)
    if any(value is None for value in target) and any(
        value is not None for value in target
    ):
        raise ParticipantRequestError(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )
    if (
        attachment_required
        and participant.attachment_ready_epoch != gate.pending_epoch
    ):
        return participant_notice(
            participant_id,
            participant,
            kind=ParticipantNoticeKind.PREPARE,
            epoch=gate.pending_epoch,
        )
    if gate.status_code is not None:
        return participant_notice(
            participant_id,
            participant,
            kind=ParticipantNoticeKind.STATUS,
            epoch=gate.pending_epoch,
            code=gate.status_code,
        )
    if (
        gate.account_id is not None
        and gate.generation is not None
        and participant.attachment_ready_epoch == gate.pending_epoch
    ):
        return participant_notice(
            participant_id,
            participant,
            kind=ParticipantNoticeKind.READY,
            epoch=gate.pending_epoch,
            operation_id=gate.operation_id,
            target_account_id=gate.account_id,
            target_generation=gate.generation,
        )
    return participant_notice(
        participant_id,
        participant,
        kind=ParticipantNoticeKind.PREPARE,
        epoch=gate.pending_epoch,
    )


def participant_notice(
    participant_id: ParticipantId,
    participant: ParticipantRecord,
    *,
    kind: ParticipantNoticeKind,
    epoch: SelectionEpoch,
    code: SelectionCode | None = None,
    operation_id: OperationId | None = None,
    target_account_id: SidekickAccountId | None = None,
    target_generation: AuthorityGeneration | None = None,
) -> ParticipantNotice:
    """Construct one secret-free notice for a registered participant."""
    return ParticipantNotice(
        participant_id=participant_id,
        provider_id=participant.manifest.provider_id,
        kind=kind,
        epoch=epoch,
        code=code,
        operation_id=operation_id,
        target_account_id=target_account_id,
        target_generation=target_generation,
    )
