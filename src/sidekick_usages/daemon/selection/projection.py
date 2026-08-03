"""Pure validation and projection for participant registry state."""

from dataclasses import dataclass, field

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
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
    confirmed_dead: bool = False
    ready_epoch: SelectionEpoch | None = None
    adopted_epoch: SelectionEpoch | None = None


@dataclass(slots=True)
class ProviderGate:
    """Passive admission data owned by the condition-owning registry."""

    pending_epoch: SelectionEpoch
    required: set[ParticipantId]
    account_id: SidekickAccountId | None = None
    generation: AuthorityGeneration | None = None
    queued: dict[TurnId, TurnBeginRequest] = field(default_factory=dict)
    sealed: bool = False
    status_code: SelectionCode | None = None


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
        and participant.connected
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


def project_notice(
    participant_id: ParticipantId,
    participant: ParticipantRecord,
    gate: ProviderGate | None,
    finalized: FinalizedSelection | None,
) -> ParticipantNotice:
    """Project the current admission state for one authenticated stream."""
    provider_id = participant.manifest.provider_id
    if gate is None:
        if finalized is None:
            raise ParticipantRequestError(
                SelectionCode.SESSION_CONFIGURATION_REQUIRED
            )
        return ParticipantNotice(
            participant_id=participant_id,
            provider_id=provider_id,
            kind=ParticipantNoticeKind.OPEN,
            epoch=finalized.epoch,
        )
    return ParticipantNotice(
        participant_id=participant_id,
        provider_id=provider_id,
        kind=(
            ParticipantNoticeKind.PREPARE
            if gate.status_code is None
            else ParticipantNoticeKind.STATUS
        ),
        epoch=gate.pending_epoch,
        code=gate.status_code,
    )
