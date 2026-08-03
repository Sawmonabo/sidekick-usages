"""Bounded in-memory participant and turn-admission registry."""

import socket
import time
from collections import deque
from collections.abc import Callable, Generator, Iterable
from threading import Condition

from sidekick_usages.core.accounts.types import (
    OperationId,
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
    MAX_ACTIVE_TURNS_PER_PROVIDER,
    MAX_PENDING_BEGINS_PER_PROVIDER,
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
from sidekick_usages.daemon.selection.ports import (
    FinalizedSelectionStore,
    ParticipantAttachmentRegistry,
    ParticipantAttachmentTransaction,
)
from sidekick_usages.daemon.selection.projection import (
    ParticipantRecord,
    ProviderGate,
    new_gate,
    participant_notice,
    project_notice,
    project_ready_notices,
    project_snapshot,
    require_capacity,
    require_connection,
    require_gate,
    require_gate_binding,
    require_gate_epoch,
    require_membership_bound,
    require_reconnect,
    require_selected,
)
from sidekick_usages.platform.models import ProcessIdentity

MAX_RETAINED_PARTICIPANT_NOTICES = 256


class ParticipantRegistry:
    """Coordinate bounded live participants without retaining user text."""

    def __init__(
        self,
        selected: FinalizedSelectionStore,
        monotonic: Callable[[], float] = time.monotonic,
        *,
        attachments: ParticipantAttachmentRegistry | None = None,
    ) -> None:
        self._selected = selected
        self._condition = Condition()
        self._participants: dict[ParticipantId, ParticipantRecord] = {}
        self._gates: dict[ProviderId, ProviderGate] = {}
        self._turns: dict[TurnId, TurnAdmission] = {}
        self._monotonic = monotonic
        self._notice_sequence = 0
        self._notices: deque[tuple[int, ParticipantNotice]] = deque(
            maxlen=MAX_RETAINED_PARTICIPANT_NOTICES
        )
        self._cancelled_subscriptions: set[RequestId] = set()
        self._attachments = () if attachments is None else (attachments,)

    def add_attachment_registry(
        self,
        attachments: ParticipantAttachmentRegistry,
    ) -> None:
        """Compose one additional provider owner before serving requests."""
        with self._condition:
            if self._participants or self._gates:
                raise RuntimeError(
                    "Participant attachment composition is already active."
                )
            if any(
                owner.requires_endpoint(provider_id)
                and attachments.requires_endpoint(provider_id)
                for owner in self._attachments
                for provider_id in ProviderId
            ):
                raise RuntimeError(
                    "Participant attachment owner is duplicate."
                )
            self._attachments = (*self._attachments, attachments)

    def requires_attachment(self, provider_id: ProviderId) -> bool:
        """Return whether an injected provider attachment is mandatory."""
        return self._attachment_registry(provider_id) is not None

    def requires_finalized_attachment(self, provider_id: ProviderId) -> bool:
        """Return whether baseline turns require a provider target bind."""
        attachments = self._attachment_registry(provider_id)
        return bool(
            attachments and attachments.requires_finalized_binding(provider_id)
        )

    def stage_attachment(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> ParticipantAttachmentTransaction:
        """Stage one injected attachment for the membership transaction."""
        attachments = self._attachment_registry(manifest.provider_id)
        if attachments is None:
            endpoint.close()
            raise ParticipantRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return attachments.stage(
            manifest.participant_id,
            manifest.connection_generation,
            peer,
            endpoint,
        )

    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        *,
        persist_required: Callable[[SelectionEpoch], None] | None = None,
        attachment: ParticipantAttachmentTransaction | None = None,
    ) -> ParticipantRegistration:
        """Persist, then register one kernel-proven participant atomically."""
        with self._condition:
            current = self._participants.get(manifest.participant_id)
            self._wait_registration_open(manifest, current)
            if current is not None:
                require_reconnect(current, manifest, peer)
            else:
                require_capacity(
                    self._participants,
                    self._gates,
                    manifest.provider_id,
                    manifest.participant_id,
                )
            selected = self._selected.load(manifest.provider_id)
            registered_epoch = (
                current.registered_epoch
                if current is not None
                else SelectionEpoch(0)
                if selected is None
                else selected.epoch
            )
            gate = self._gates.get(manifest.provider_id)
            protected = self.requires_attachment(manifest.provider_id)
            attachment_ready_epoch = (
                gate.pending_epoch
                if gate and gate.account_id and not protected
                else registered_epoch
                if attachment is not None
                and gate is None
                and not self.requires_finalized_attachment(
                    manifest.provider_id
                )
                else None
            )
            registration = ParticipantRegistration(
                participant_id=manifest.participant_id,
                provider_id=manifest.provider_id,
                connection_generation=manifest.connection_generation,
                registered_epoch=registered_epoch,
                pending_epoch=None if gate is None else gate.pending_epoch,
            )
            required = None
            if gate is not None:
                required = gate.required | {manifest.participant_id}
                require_membership_bound(
                    self._participants,
                    manifest.provider_id,
                    required,
                )
            if gate is not None and persist_required is not None:
                persist_required(gate.pending_epoch)
            if attachment is not None:
                attachment.commit()
            try:
                if current is not None:
                    current.manifest = manifest
                    current.confirmed_dead = False
                    current.attachment_ready_epoch = attachment_ready_epoch
                    current.ready_epoch = None
                else:
                    current = ParticipantRecord(
                        manifest,
                        peer,
                        registered_epoch,
                        attachment_ready_epoch=attachment_ready_epoch,
                    )
                    self._participants[manifest.participant_id] = current
                if gate is not None and required is not None:
                    gate.required = required
                    self._append_notice(
                        manifest.participant_id,
                        ParticipantNoticeKind.PREPARE,
                        gate.pending_epoch,
                    )
            except BaseException:
                if attachment is not None:
                    attachment.rollback()
                raise
            if attachment is not None:
                attachment.finalize()
            self._condition.notify_all()
            return registration

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
            selected = require_selected(self._selected.load(provider_id))
            if self.requires_attachment(provider_id) and (
                participant.attachment_ready_epoch != selected.epoch
            ):
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
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
        operation_id: OperationId,
        pending_epoch: SelectionEpoch,
    ) -> ParticipantSnapshot:
        """Close new-turn admission and capture live required clients."""
        with self._condition:
            if provider_id in self._gates:
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            require_gate_epoch(self._selected.load(provider_id), pending_epoch)
            required = {
                participant_id
                for participant_id, participant in self._participants.items()
                if participant.manifest.provider_id is provider_id
                and not participant.confirmed_dead
            }
            self._gates[provider_id] = new_gate(
                operation_id, pending_epoch, required
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
        operation_id: OperationId,
        pending_epoch: SelectionEpoch,
        target_account_id: SidekickAccountId,
        required_participant_ids: tuple[ParticipantId, ...],
    ) -> ParticipantSnapshot:
        """Restore one crash-recovery gate from opaque durable IDs."""
        with self._condition:
            require_gate_epoch(
                self._selected.load(provider_id),
                pending_epoch,
                recovery_target=target_account_id,
            )
            current = self._gates.get(provider_id)
            required = set(required_participant_ids) | (
                set() if current is None else current.required
            )
            require_membership_bound(self._participants, provider_id, required)
            if current is not None:
                require_gate_binding(current, operation_id, pending_epoch)
                current.required = required
                return self._snapshot(provider_id)
            self._gates[provider_id] = new_gate(
                operation_id, pending_epoch, required
            )
            return self._snapshot(provider_id)

    def prepare_target(
        self, operation_id: OperationId, proof: AuthorityReadyProof
    ) -> bool:
        """Bind participant readiness to exact provider commit proof."""
        with self._condition:
            gate = require_gate(self._gates, proof.provider_id)
            project_ready_notices(
                operation_id, proof, gate, self._participants
            )
            installed = self._prepare_attachments(
                gate.required, operation_id, proof
            )
            notices = project_ready_notices(
                operation_id, proof, gate, self._participants
            )
            gate.account_id = proof.account_id
            gate.generation = proof.generation
            for notice in notices:
                self._retain_notice(notice)
            self._condition.notify_all()
            return self.requires_attachment(proof.provider_id) and bool(
                installed
            )

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
            gate = require_gate(self._gates, participant.manifest.provider_id)
            if (
                participant_id not in gate.required
                or participant.attachment_ready_epoch != proof.epoch
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
            attachments = self._attachment_registry(
                participant.manifest.provider_id
            )
            if attachments is not None:
                attachments.remove(
                    participant_id,
                    participant.manifest.connection_generation,
                    peer,
                )
            participant.connected = False
            participant.attachment_ready_epoch = None
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
        """Return live and durably required provider membership."""
        with self._condition:
            return self._snapshot(provider_id).registered_count

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

        def current_notice() -> ParticipantNotice:
            participant = self._participants[request.participant_id]
            provider_id = participant.manifest.provider_id
            return project_notice(
                request.participant_id,
                participant,
                self._gates.get(provider_id),
                self._selected.load(provider_id),
                attachment_required=self.requires_finalized_attachment(
                    provider_id
                ),
            )

        with self._condition:
            if request_id in self._cancelled_subscriptions:
                self._cancelled_subscriptions.discard(request_id)
                return
            self._wait_unsealed_for_participant(request.participant_id)
            participant = self._require_connection(
                request.participant_id,
                request.connection_generation,
            )
            if participant.connected:
                raise ParticipantRequestError(
                    SelectionCode.PARTICIPANT_UNREACHABLE
                )
            cursor = self._notice_sequence
            initial = current_notice()
            participant.connected = True
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
                            notice = current_notice()
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

    def target_prepared(self, provider_id: ProviderId) -> bool:
        """Return whether every live obligation installed the target."""
        with self._condition:
            gate = require_gate(self._gates, provider_id)
            return all(
                (participant := self._participants.get(participant_id))
                is not None
                and (
                    participant.confirmed_dead
                    or participant.attachment_ready_epoch == gate.pending_epoch
                )
                for participant_id in gate.required
            )

    def prepare_finalized(
        self,
        operation_id: OperationId,
        finalized: FinalizedSelection,
    ) -> None:
        """Open only participants that installed exact finalized authority."""
        with self._condition:
            if self._selected.load(finalized.provider_id) != finalized:
                raise ParticipantRequestError(
                    SelectionCode.AUTHORITY_PROOF_FAILED
                )
            if not self.requires_finalized_attachment(finalized.provider_id):
                return
            records = self._participants
            installed = self._prepare_attachments(
                (
                    participant_id
                    for participant_id, participant in records.items()
                    if participant.manifest.provider_id
                    is finalized.provider_id
                ),
                operation_id,
                finalized,
            )
            self._open_participants(installed, finalized.epoch)
            self._condition.notify_all()

    def seal_ready(self, provider_id: ProviderId) -> ParticipantSnapshot:
        """Freeze resolved membership through the finalization write window."""
        with self._condition:
            gate = require_gate(self._gates, provider_id)
            if not self._all_required_resolved(provider_id):
                raise ParticipantRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            gate.sealed = True
            return self._snapshot(provider_id)

    def seal_precommit(self, provider_id: ProviderId) -> ParticipantSnapshot:
        """Freeze membership after old work and reachability are proven."""
        with self._condition:
            gate = require_gate(self._gates, provider_id)
            snapshot = self._snapshot(provider_id)
            if snapshot.active_turn_count or (
                snapshot.unreachable_participant_ids
            ):
                raise ParticipantRequestError(
                    SelectionCode.PARTICIPANT_UNREACHABLE
                )
            gate.membership_sealed = True
            return snapshot

    def unseal(self, provider_id: ProviderId) -> None:
        """Allow late registration after failed finalization stays gated."""
        with self._condition:
            gate = require_gate(self._gates, provider_id)
            gate.membership_sealed = False
            gate.sealed = False
            self._condition.notify_all()

    def open_admission(
        self,
        provider_id: ProviderId,
        epoch: SelectionEpoch,
    ) -> tuple[ParticipantId, ...]:
        """Open one finalized epoch without transmitting queued prompts."""
        with self._condition:
            gate = require_gate(self._gates, provider_id)
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
            self._open_participants(gate.required, epoch)
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
            self._open_participants(gate.required, epoch)
            self._gates.pop(provider_id)
            self._condition.notify_all()

    def _snapshot(self, provider_id: ProviderId) -> ParticipantSnapshot:
        gate = self._gates.get(provider_id)
        return project_snapshot(
            provider_id,
            self._participants,
            self._turns,
            gate,
            self._selected.load(provider_id),
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

    def _wait_unsealed(self, provider_id: ProviderId) -> None:
        while (
            gate := self._gates.get(provider_id)
        ) is not None and gate.sealed:
            self._condition.wait()

    def _wait_registration_open(
        self,
        manifest: ParticipantManifest,
        current: ParticipantRecord | None,
    ) -> None:
        while gate := self._gates.get(manifest.provider_id):
            if gate.sealed:
                self._condition.wait()
                continue
            reconnecting_required = (
                current is not None
                and manifest.participant_id in gate.required
            )
            if not gate.membership_sealed or reconnecting_required:
                return
            self._condition.wait()

    def _wait_unsealed_for_participant(
        self,
        participant_id: ParticipantId,
    ) -> None:
        if (participant := self._participants.get(participant_id)) is not None:
            self._wait_unsealed(participant.manifest.provider_id)

    def _prepare_attachments(
        self,
        participant_ids: Iterable[ParticipantId],
        operation_id: OperationId,
        authority: AuthorityReadyProof | FinalizedSelection,
    ) -> tuple[ParticipantId, ...]:
        installed: list[ParticipantId] = []
        for participant_id in participant_ids:
            participant = self._participants.get(participant_id)
            if participant is None:
                continue
            attachments = self._attachment_registry(
                participant.manifest.provider_id
            )
            protected = attachments is not None
            arguments = (
                participant_id,
                participant.manifest.connection_generation,
                participant.process_identity,
                operation_id,
            )
            matched = not protected or (
                attachments.matches_target(*arguments, authority)
                if isinstance(authority, AuthorityReadyProof)
                else attachments.matches_finalized(*arguments, authority)
            )
            if matched:
                participant.attachment_ready_epoch = authority.epoch
                installed.append(participant_id)
        return tuple(installed)

    def _attachment_registry(
        self,
        provider_id: ProviderId,
    ) -> ParticipantAttachmentRegistry | None:
        for attachments in self._attachments:
            if attachments.requires_endpoint(provider_id):
                return attachments
        return None

    def _open_participants(
        self,
        participant_ids: Iterable[ParticipantId],
        epoch: SelectionEpoch,
    ) -> None:
        for participant_id in participant_ids:
            participant = self._participants.get(participant_id)
            if participant is None:
                continue
            if participant.confirmed_dead:
                self._participants.pop(participant_id)
            elif participant.connected:
                participant.registered_epoch = epoch
                self._append_notice(
                    participant_id, ParticipantNoticeKind.OPEN, epoch
                )

    def _append_notice(
        self,
        participant_id: ParticipantId,
        kind: ParticipantNoticeKind,
        epoch: SelectionEpoch,
        code: SelectionCode | None = None,
    ) -> None:
        participant = self._participants[participant_id]
        notice = participant_notice(
            participant_id, participant, kind=kind, epoch=epoch, code=code
        )
        self._retain_notice(notice)

    def _retain_notice(self, notice: ParticipantNotice) -> None:
        """Append one already-projected notice to the bounded queue."""
        self._notice_sequence += 1
        self._notices.append((self._notice_sequence, notice))

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
    ) -> ParticipantRecord:
        return require_connection(
            self._participants, participant_id, connection_generation
        )
