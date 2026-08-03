"""Bounded in-memory participant and turn-admission registry."""

import time
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from threading import Condition

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
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
    ParticipantAdoptionProof,
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRegistration,
    ParticipantRequestError,
    ParticipantSnapshot,
    TurnAdmission,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.ports import FinalizedSelectionStore
from sidekick_usages.platform.models import ProcessIdentity

MAX_PARTICIPANTS_PER_PROVIDER = 16
MAX_ACTIVE_TURNS_PER_PROVIDER = 128
MAX_PENDING_BEGINS_PER_PROVIDER = 128
MAX_RETAINED_PARTICIPANT_NOTICES = 256


@dataclass(slots=True)
class _Participant:
    manifest: ParticipantManifest
    process_identity: ProcessIdentity
    registered_epoch: SelectionEpoch
    connected: bool = False
    confirmed_dead: bool = False
    ready_epoch: SelectionEpoch | None = None
    adopted_epoch: SelectionEpoch | None = None


@dataclass(slots=True)
class _ProviderGate:
    pending_epoch: SelectionEpoch
    required: set[ParticipantId]
    account_id: SidekickAccountId | None = None
    generation: AuthorityGeneration | None = None
    queued: dict[TurnId, TurnBeginRequest] = field(default_factory=dict)
    sealed: bool = False
    status_code: SelectionCode | None = None


class ParticipantRegistry:
    """Coordinate bounded live participants without retaining user text."""

    def __init__(
        self,
        selected: FinalizedSelectionStore,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._selected = selected
        self._condition = Condition()
        self._participants: dict[ParticipantId, _Participant] = {}
        self._gates: dict[ProviderId, _ProviderGate] = {}
        self._turns: dict[TurnId, TurnAdmission] = {}
        self._monotonic = monotonic
        self._notice_sequence = 0
        self._notices: deque[tuple[int, ParticipantNotice]] = deque(
            maxlen=MAX_RETAINED_PARTICIPANT_NOTICES
        )
        self._cancelled_subscriptions: set[RequestId] = set()

    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        *,
        persist_required: Callable[[SelectionEpoch], None] | None = None,
    ) -> ParticipantRegistration:
        """Persist, then register one kernel-proven participant atomically."""
        with self._condition:
            self._wait_unsealed(manifest.provider_id)
            current = self._participants.get(manifest.participant_id)
            if current is not None:
                self._require_reconnect(current, manifest, peer)
            else:
                self._require_capacity(manifest.provider_id)
            selected = self._selected.load(manifest.provider_id)
            registered_epoch = (
                current.registered_epoch
                if current is not None
                else SelectionEpoch(0)
                if selected is None
                else selected.epoch
            )
            gate = self._gates.get(manifest.provider_id)
            if gate is not None and persist_required is not None:
                persist_required(gate.pending_epoch)
            if current is not None:
                current.manifest = manifest
                current.confirmed_dead = False
                current.ready_epoch = None
            else:
                current = _Participant(manifest, peer, registered_epoch)
                self._participants[manifest.participant_id] = current
            if gate is not None:
                gate.required.add(manifest.participant_id)
                self._append_notice(
                    manifest.participant_id,
                    ParticipantNoticeKind.PREPARE,
                    gate.pending_epoch,
                )
            self._condition.notify_all()
            return ParticipantRegistration(
                participant_id=manifest.participant_id,
                provider_id=manifest.provider_id,
                connection_generation=manifest.connection_generation,
                registered_epoch=current.registered_epoch,
                pending_epoch=None if gate is None else gate.pending_epoch,
            )

    def begin_turn(self, request: TurnBeginRequest) -> TurnAdmission:
        """Admit one exact turn or retain only its bounded begin metadata."""
        with self._condition:
            participant = self._require_connection(
                request.participant_id,
                request.connection_generation,
            )
            existing = self._turns.get(request.turn_id)
            if existing is not None:
                if existing.participant_id != request.participant_id:
                    raise ParticipantRequestError(
                        SelectionCode.AUTHORITY_PROOF_FAILED
                    )
                return existing
            provider_id = participant.manifest.provider_id
            gate = self._gates.get(provider_id)
            if gate is not None:
                existing_request = gate.queued.get(request.turn_id)
                if (
                    existing_request is not None
                    and existing_request != request
                ):
                    raise ParticipantRequestError(
                        SelectionCode.AUTHORITY_PROOF_FAILED
                    )
                if (
                    existing_request is None
                    and len(gate.queued) >= MAX_PENDING_BEGINS_PER_PROVIDER
                ):
                    raise ParticipantRequestError(
                        SelectionCode.ACTIVE_OPERATION_TIMEOUT
                    )
                gate.queued[request.turn_id] = request
                return TurnAdmission(
                    participant_id=request.participant_id,
                    turn_id=request.turn_id,
                    state=TurnAdmissionState.QUEUED,
                    epoch=None,
                    account_id=None,
                    generation=None,
                )
            selected = self._require_selected(provider_id)
            if (
                self._active_turn_count(provider_id)
                >= MAX_ACTIVE_TURNS_PER_PROVIDER
            ):
                raise ParticipantRequestError(
                    SelectionCode.ACTIVE_OPERATION_TIMEOUT
                )
            admission = TurnAdmission(
                participant_id=request.participant_id,
                turn_id=request.turn_id,
                state=TurnAdmissionState.ADMITTED,
                epoch=selected.epoch,
                account_id=selected.account_id,
                generation=selected.generation,
            )
            self._turns[request.turn_id] = admission
            return admission

    def require_peer(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
    ) -> ProviderId:
        """Require the exact registered process for one participant action."""
        with self._condition:
            participant = self._require_connection(
                participant_id,
                connection_generation,
            )
            if participant.process_identity != peer:
                raise ParticipantRequestError(
                    SelectionCode.PARTICIPANT_UNREACHABLE
                )
            return participant.manifest.provider_id

    def end_turn(self, request: TurnEndRequest) -> None:
        """End only the exact participant-owned admitted turn."""
        with self._condition:
            self._require_connection(
                request.participant_id,
                request.connection_generation,
            )
            admission = self._turns.get(request.turn_id)
            if (
                admission is None
                or admission.participant_id != request.participant_id
            ):
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            self._turns.pop(request.turn_id)
            self._condition.notify_all()

    def close_admission(
        self,
        provider_id: ProviderId,
        pending_epoch: SelectionEpoch,
    ) -> ParticipantSnapshot:
        """Close new-turn admission and capture live required clients."""
        with self._condition:
            if provider_id in self._gates:
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            required = {
                participant_id
                for participant_id, participant in self._participants.items()
                if participant.manifest.provider_id is provider_id
                and not participant.confirmed_dead
            }
            self._gates[provider_id] = _ProviderGate(
                pending_epoch,
                required,
            )
            for participant_id in required:
                self._append_notice(
                    participant_id,
                    ParticipantNoticeKind.PREPARE,
                    pending_epoch,
                )
            self._condition.notify_all()
            return self._snapshot(provider_id)

    def restore_admission(
        self,
        provider_id: ProviderId,
        pending_epoch: SelectionEpoch,
        required_participant_ids: tuple[ParticipantId, ...],
    ) -> ParticipantSnapshot:
        """Restore one crash-recovery gate from opaque durable IDs."""
        with self._condition:
            current = self._gates.get(provider_id)
            if current is not None:
                if current.pending_epoch != pending_epoch:
                    raise ParticipantRequestError(
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                    )
                current.required.update(required_participant_ids)
                return self._snapshot(provider_id)
            self._gates[provider_id] = _ProviderGate(
                pending_epoch,
                set(required_participant_ids),
            )
            return self._snapshot(provider_id)

    def prepare_target(self, proof: AuthorityReadyProof) -> None:
        """Bind participant readiness to exact provider commit proof."""
        with self._condition:
            gate = self._require_gate(proof.provider_id)
            if gate.pending_epoch != proof.epoch:
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            gate.account_id = proof.account_id
            gate.generation = proof.generation
            self._condition.notify_all()

    def ready(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantReadyProof,
    ) -> None:
        """Record one exact next-turn readiness acknowledgement."""
        with self._condition:
            self._wait_unsealed_for_participant(participant_id)
            participant = self._require_connection(
                participant_id,
                connection_generation,
            )
            gate = self._require_gate(participant.manifest.provider_id)
            if (
                participant_id not in gate.required
                or gate.account_id != proof.account_id
                or gate.generation != proof.generation
                or gate.pending_epoch != proof.epoch
            ):
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            participant.ready_epoch = proof.epoch
            self._condition.notify_all()

    def adopt(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantAdoptionProof,
    ) -> None:
        """Record ephemeral adoption by one real admitted turn."""
        with self._condition:
            participant = self._require_connection(
                participant_id,
                connection_generation,
            )
            admission = self._turns.get(proof.turn_id)
            if (
                admission is None
                or admission.participant_id != participant_id
                or admission.epoch != proof.epoch
                or admission.account_id != proof.account_id
                or admission.generation != proof.generation
            ):
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            participant.adopted_epoch = proof.epoch

    def ready_request(self, request: ParticipantReadyRequest) -> None:
        """Record one already-authenticated readiness request."""
        self.ready(
            request.participant_id,
            request.connection_generation,
            request.proof,
        )

    def adopt_request(self, request: ParticipantAdoptionRequest) -> None:
        """Record one already-authenticated adoption request."""
        self.adopt(
            request.participant_id,
            request.connection_generation,
            request.proof,
        )

    def disconnect(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        """Mark one exact connection unreachable without assuming death."""
        with self._condition:
            self._wait_unsealed_for_participant(participant_id)
            participant = self._require_connection(
                participant_id,
                connection_generation,
            )
            participant.connected = False
            participant.ready_epoch = None
            self._condition.notify_all()

    def confirm_dead(
        self,
        participant_id: ParticipantId,
        peer: ProcessIdentity,
    ) -> None:
        """Confirm death only for one exact process-start identity."""
        with self._condition:
            self._wait_unsealed_for_participant(participant_id)
            participant = self._participants.get(participant_id)
            if participant is None or participant.process_identity != peer:
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            participant.connected = False
            participant.confirmed_dead = True
            participant.ready_epoch = None
            self._turns = {
                turn_id: admission
                for turn_id, admission in self._turns.items()
                if admission.participant_id != participant_id
            }
            gate = self._gates.get(participant.manifest.provider_id)
            if gate is None or participant_id not in gate.required:
                self._participants.pop(participant_id)
            self._condition.notify_all()

    def snapshot(self, provider_id: ProviderId) -> ParticipantSnapshot:
        """Return one secret-free provider participant snapshot."""
        with self._condition:
            return self._snapshot(provider_id)

    def registered_count(self, provider_id: ProviderId) -> int:
        """Return the bounded number of retained provider participants."""
        with self._condition:
            return sum(
                participant.manifest.provider_id is provider_id
                for participant in self._participants.values()
            )

    def unreachable_processes(
        self,
        provider_id: ProviderId,
    ) -> tuple[tuple[ParticipantId, ProcessIdentity], ...]:
        """Return exact disconnected process identities for inspection."""
        with self._condition:
            return tuple(
                (participant_id, participant.process_identity)
                for participant_id, participant in self._participants.items()
                if participant.manifest.provider_id is provider_id
                and not participant.connected
                and not participant.confirmed_dead
            )

    def unresolved_processes(
        self,
        provider_id: ProviderId,
    ) -> tuple[tuple[ParticipantId, ProcessIdentity], ...]:
        """Return exact identities for unresolved required participants."""
        with self._condition:
            gate = self._gates.get(provider_id)
            if gate is None:
                return ()
            return tuple(
                (participant_id, participant.process_identity)
                for participant_id in gate.required
                if (participant := self._participants.get(participant_id))
                is not None
                and participant.ready_epoch != gate.pending_epoch
                and not participant.confirmed_dead
            )

    def subscribe(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
    ) -> Generator[ParticipantNotice]:
        """Yield future bounded participant notices until cancellation."""
        with self._condition:
            self._wait_unsealed_for_participant(request.participant_id)
            participant = self._require_connection(
                request.participant_id,
                request.connection_generation,
            )
            if participant.connected:
                raise ParticipantRequestError(
                    SelectionCode.PARTICIPANT_UNREACHABLE
                )
            participant.connected = True
            cursor = self._notice_sequence
            initial = self._current_notice(request.participant_id)
            self._condition.notify_all()
        try:
            yield initial
            while True:
                with self._condition:
                    while request_id not in self._cancelled_subscriptions:
                        if self._notices and (
                            cursor < self._notices[0][0] - 1
                        ):
                            cursor = self._notice_sequence
                            notice = self._current_notice(
                                request.participant_id
                            )
                            break
                        match = next(
                            (
                                (sequence, notice)
                                for sequence, notice in self._notices
                                if sequence > cursor
                                and notice.participant_id
                                == request.participant_id
                            ),
                            None,
                        )
                        if match is not None:
                            cursor, notice = match
                            break
                        self._condition.wait()
                    else:
                        return
                yield notice
        finally:
            with self._condition:
                self._cancelled_subscriptions.discard(request_id)

    def cancel_subscription(self, request_id: RequestId) -> None:
        """Cancel one disconnected notice stream without changing gates."""
        with self._condition:
            self._cancelled_subscriptions.add(request_id)
            self._condition.notify_all()

    def publish_status(
        self,
        provider_id: ProviderId,
        epoch: SelectionEpoch,
        code: SelectionCode,
    ) -> None:
        """Publish one typed provider selection status to live clients."""
        with self._condition:
            gate = self._gates.get(provider_id)
            if gate is not None:
                gate.status_code = code
            for participant_id, participant in self._participants.items():
                if (
                    participant.manifest.provider_id is provider_id
                    and participant.connected
                ):
                    self._append_notice(
                        participant_id,
                        ParticipantNoticeKind.STATUS,
                        epoch,
                        code,
                    )
            self._condition.notify_all()

    def prune_confirmed_dead(
        self,
        provider_id: ProviderId,
        participant_ids: tuple[ParticipantId, ...],
    ) -> None:
        """Forget exact dead clients after journal truth is retained."""
        with self._condition:
            gate = self._gates.get(provider_id)
            for participant_id in participant_ids:
                participant = self._participants.get(participant_id)
                if (
                    participant is None
                    or participant.manifest.provider_id is not provider_id
                    or not participant.confirmed_dead
                ):
                    raise ParticipantRequestError(
                        SelectionCode.AUTHORITY_PROOF_FAILED
                    )
                self._participants.pop(participant_id)
                if gate is not None:
                    gate.required.discard(participant_id)
                    gate.queued = {
                        turn_id: request
                        for turn_id, request in gate.queued.items()
                        if request.participant_id != participant_id
                    }
            self._condition.notify_all()

    def wait_for_old_turns(
        self,
        provider_id: ProviderId,
        timeout_seconds: float,
    ) -> bool:
        """Wait without polling until every admitted provider turn ends."""
        with self._condition:
            return self._wait_until(
                lambda: self._active_turn_count(provider_id) == 0,
                timeout_seconds,
            )

    def wait_for_ready(
        self,
        provider_id: ProviderId,
        timeout_seconds: float,
    ) -> bool:
        """Wait until every required client is ready or proven dead."""
        with self._condition:
            return self._wait_until(
                lambda: self._all_required_resolved(provider_id),
                timeout_seconds,
            )

    def ready_resolved(self, provider_id: ProviderId) -> bool:
        """Return whether every required participant has exact resolution."""
        with self._condition:
            return self._all_required_resolved(provider_id)

    def seal_ready(self, provider_id: ProviderId) -> ParticipantSnapshot:
        """Freeze resolved membership through the finalization write window."""
        with self._condition:
            gate = self._require_gate(provider_id)
            if not self._all_required_resolved(provider_id):
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            gate.sealed = True
            return self._snapshot(provider_id)

    def seal_precommit(self, provider_id: ProviderId) -> ParticipantSnapshot:
        """Freeze membership after old work and reachability are proven."""
        with self._condition:
            gate = self._require_gate(provider_id)
            snapshot = self._snapshot(provider_id)
            if snapshot.active_turn_count or (
                snapshot.unreachable_participant_ids
            ):
                raise ParticipantRequestError(
                    SelectionCode.PARTICIPANT_UNREACHABLE
                )
            gate.sealed = True
            return snapshot

    def unseal(self, provider_id: ProviderId) -> None:
        """Allow late registration after failed finalization stays gated."""
        with self._condition:
            gate = self._require_gate(provider_id)
            gate.sealed = False
            self._condition.notify_all()

    def open_admission(
        self,
        provider_id: ProviderId,
        epoch: SelectionEpoch,
    ) -> tuple[ParticipantId, ...]:
        """Open one finalized epoch without transmitting queued prompts."""
        with self._condition:
            gate = self._require_gate(provider_id)
            if gate.pending_epoch != epoch or not self._all_required_resolved(
                provider_id
            ):
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            released = tuple(
                sorted(
                    {
                        request.participant_id
                        for request in gate.queued.values()
                    }
                )
            )
            for participant_id in gate.required:
                participant = self._participants.get(participant_id)
                if participant is None:
                    continue
                if participant.connected:
                    participant.registered_epoch = epoch
                    self._append_notice(
                        participant_id,
                        ParticipantNoticeKind.OPEN,
                        epoch,
                    )
            self._gates.pop(provider_id)
            self._condition.notify_all()
            return released

    def reopen_baseline(self, provider_id: ProviderId) -> None:
        """Reopen unchanged admission after a proven precommit failure."""
        with self._condition:
            gate = self._gates.get(provider_id)
            if gate is None:
                return
            selected = self._selected.load(provider_id)
            epoch = SelectionEpoch(0) if selected is None else selected.epoch
            for participant_id in gate.required:
                participant = self._participants.get(participant_id)
                if participant is None:
                    continue
                if participant.confirmed_dead:
                    self._participants.pop(participant_id)
                    continue
                if participant.connected:
                    participant.registered_epoch = epoch
                    self._append_notice(
                        participant_id,
                        ParticipantNoticeKind.OPEN,
                        epoch,
                    )
            self._gates.pop(provider_id)
            self._condition.notify_all()

    def _snapshot(self, provider_id: ProviderId) -> ParticipantSnapshot:
        gate = self._gates.get(provider_id)
        required = set() if gate is None else set(gate.required)
        pending_epoch = None if gate is None else gate.pending_epoch
        ready = {
            participant_id
            for participant_id in required
            if participant_id in self._participants
            and self._participants[participant_id].ready_epoch == pending_epoch
        }
        dead = {
            participant_id
            for participant_id in required
            if participant_id in self._participants
            and self._participants[participant_id].confirmed_dead
        }
        unreachable = {
            participant_id
            for participant_id in required
            if participant_id not in self._participants
            or (
                not self._participants[participant_id].connected
                and participant_id not in dead
            )
        }
        return ParticipantSnapshot(
            provider_id=provider_id,
            required_participant_ids=tuple(sorted(required)),
            ready_participant_ids=tuple(sorted(ready)),
            confirmed_dead_participant_ids=tuple(sorted(dead)),
            unreachable_participant_ids=tuple(sorted(unreachable)),
            active_turn_count=self._active_turn_count(provider_id),
            registered_count=sum(
                participant.manifest.provider_id is provider_id
                for participant in self._participants.values()
            ),
            reachable_count=sum(
                participant.manifest.provider_id is provider_id
                and participant.connected
                and not participant.confirmed_dead
                for participant in self._participants.values()
            ),
            adopted_count=sum(
                participant.manifest.provider_id is provider_id
                and participant.adopted_epoch is not None
                for participant in self._participants.values()
            ),
            queued_turn_count=0 if gate is None else len(gate.queued),
        )

    def _all_required_resolved(self, provider_id: ProviderId) -> bool:
        snapshot = self._snapshot(provider_id)
        return set(snapshot.required_participant_ids) == set(
            snapshot.ready_participant_ids
        ) | set(snapshot.confirmed_dead_participant_ids)

    def _active_turn_count(self, provider_id: ProviderId) -> int:
        return sum(
            self._participants[turn.participant_id].manifest.provider_id
            is provider_id
            for turn in self._turns.values()
        )

    def _has_active_turn(self, participant_id: ParticipantId) -> bool:
        return any(
            turn.participant_id == participant_id
            for turn in self._turns.values()
        )

    def _wait_unsealed(self, provider_id: ProviderId) -> None:
        while (
            gate := self._gates.get(provider_id)
        ) is not None and gate.sealed:
            self._condition.wait()

    def _wait_unsealed_for_participant(
        self,
        participant_id: ParticipantId,
    ) -> None:
        participant = self._participants.get(participant_id)
        if participant is not None:
            self._wait_unsealed(participant.manifest.provider_id)

    def _append_notice(
        self,
        participant_id: ParticipantId,
        kind: ParticipantNoticeKind,
        epoch: SelectionEpoch,
        code: SelectionCode | None = None,
    ) -> None:
        participant = self._participants[participant_id]
        self._notice_sequence += 1
        self._notices.append(
            (
                self._notice_sequence,
                ParticipantNotice(
                    participant_id=participant_id,
                    provider_id=participant.manifest.provider_id,
                    kind=kind,
                    epoch=epoch,
                    code=code,
                ),
            )
        )

    def _current_notice(
        self,
        participant_id: ParticipantId,
    ) -> ParticipantNotice:
        """Return the current semantic state after connect or overrun."""
        participant = self._participants[participant_id]
        provider_id = participant.manifest.provider_id
        gate = self._gates.get(provider_id)
        if gate is None:
            return ParticipantNotice(
                participant_id=participant_id,
                provider_id=provider_id,
                kind=ParticipantNoticeKind.OPEN,
                epoch=self._require_selected(provider_id).epoch,
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

    def _wait_until(
        self,
        predicate: Callable[[], bool],
        timeout_seconds: float,
    ) -> bool:
        deadline = self._monotonic() + timeout_seconds
        while not predicate():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._condition.wait(remaining)
        return True

    def _require_connection(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> _Participant:
        participant = self._participants.get(participant_id)
        if (
            participant is None
            or participant.confirmed_dead
            or participant.manifest.connection_generation
            != connection_generation
        ):
            raise ParticipantRequestError(
                SelectionCode.PARTICIPANT_UNREACHABLE
            )
        return participant

    def _require_gate(self, provider_id: ProviderId) -> _ProviderGate:
        gate = self._gates.get(provider_id)
        if gate is None:
            raise ParticipantRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        return gate

    def _require_selected(self, provider_id: ProviderId) -> FinalizedSelection:
        selected = self._selected.load(provider_id)
        if selected is None:
            raise ParticipantRequestError(
                SelectionCode.SESSION_CONFIGURATION_REQUIRED
            )
        return selected

    def _require_capacity(self, provider_id: ProviderId) -> None:
        count = sum(
            participant.manifest.provider_id is provider_id
            for participant in self._participants.values()
        )
        if count >= MAX_PARTICIPANTS_PER_PROVIDER:
            raise ParticipantRequestError(
                SelectionCode.ACTIVE_OPERATION_TIMEOUT
            )

    @staticmethod
    def _require_reconnect(
        current: _Participant,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
    ) -> None:
        if (
            current.connected
            or current.process_identity != peer
            or current.manifest.provider_id is not manifest.provider_id
            or current.manifest.client_kind != manifest.client_kind
            or current.manifest.capability_version
            != manifest.capability_version
            or manifest.connection_generation
            <= current.manifest.connection_generation
        ):
            raise ParticipantRequestError(
                SelectionCode.PARTICIPANT_UNREACHABLE
            )
