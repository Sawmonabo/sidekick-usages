"""Bounded turn-admission state owned by the participant registry."""

from collections.abc import Iterable

from sidekick_usages.core.selection.models import FinalizedSelection
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.daemon.selection.models import (
    MAX_ACTIVE_TURNS_PER_PROVIDER,
    MAX_PENDING_BEGINS_PER_PROVIDER,
    ParticipantRequestError,
    TurnAdmission,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
    TurnResumeRequest,
)
from sidekick_usages.daemon.selection.projection import ProviderGate

_ACTIVE_OPERATION_TIMEOUT = SelectionCode.ACTIVE_OPERATION_TIMEOUT
_AUTHORITY_PROOF_FAILED = SelectionCode.AUTHORITY_PROOF_FAILED
_SELECTION_RECOVERY_REQUIRED = SelectionCode.SELECTION_RECOVERY_REQUIRED
_SESSION_CONFIGURATION_REQUIRED = SelectionCode.SESSION_CONFIGURATION_REQUIRED


class ParticipantTurnRegistry:
    """Own admitted turns and bounded selection-boundary begins."""

    def __init__(self) -> None:
        self._admissions: dict[TurnId, TurnAdmission] = {}

    def begin(
        self,
        request: TurnBeginRequest,
        gate: ProviderGate | None,
        selected: FinalizedSelection | None,
        *,
        attachment_ready: bool,
        active_turn_count: int,
    ) -> TurnAdmission:
        """Admit one exact turn or queue its bounded begin metadata."""
        existing = self._admissions.get(request.turn_id)
        if existing is not None:
            if existing.participant_id != request.participant_id:
                raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
            return existing
        if gate is not None:
            return self._queue(request, gate)
        if selected is None:
            raise ParticipantRequestError(_SESSION_CONFIGURATION_REQUIRED)
        if not attachment_ready:
            raise ParticipantRequestError(_SELECTION_RECOVERY_REQUIRED)
        self._require_capacity(active_turn_count)
        admission = TurnAdmission(
            participant_id=request.participant_id,
            turn_id=request.turn_id,
            state=TurnAdmissionState.ADMITTED,
            epoch=selected.epoch,
            account_id=selected.account_id,
            generation=selected.generation,
        )
        self._admissions[request.turn_id] = admission
        return admission

    def resume(
        self,
        request: TurnResumeRequest,
        selected: FinalizedSelection | None,
        active_turn_count: int,
    ) -> TurnAdmission:
        """Reconstruct one exact old turn after participant reconnect."""
        admission = request.admission
        if selected is None or (
            admission.account_id != selected.account_id
            or admission.generation != selected.generation
            or admission.epoch != selected.epoch
        ):
            raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
        existing = self._admissions.get(admission.turn_id)
        if existing is not None:
            if existing != admission:
                raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
            return existing
        self._require_capacity(active_turn_count)
        self._admissions[admission.turn_id] = admission
        return admission

    def end(self, request: TurnEndRequest) -> None:
        """End only the exact participant-owned admitted turn."""
        admission = self._admissions.get(request.turn_id)
        if admission is None:
            return
        if admission.participant_id != request.participant_id:
            raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
        self._admissions.pop(request.turn_id)

    def get(self, turn_id: TurnId) -> TurnAdmission | None:
        """Return one active admission without exposing mutable storage."""
        return self._admissions.get(turn_id)

    def remove_participant(self, participant_id: ParticipantId) -> None:
        """Remove every admission owned by one proven-dead participant."""
        self._admissions = {
            turn_id: admission
            for turn_id, admission in self._admissions.items()
            if admission.participant_id != participant_id
        }

    def values(self) -> Iterable[TurnAdmission]:
        """Return the live admission view for secret-free projection."""
        return self._admissions.values()

    @staticmethod
    def _queue(
        request: TurnBeginRequest,
        gate: ProviderGate,
    ) -> TurnAdmission:
        existing = gate.queued.get(request.turn_id)
        if existing is not None and existing != request:
            raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
        if (
            existing is None
            and len(gate.queued) >= MAX_PENDING_BEGINS_PER_PROVIDER
        ):
            raise ParticipantRequestError(_ACTIVE_OPERATION_TIMEOUT)
        gate.queued[request.turn_id] = request
        return TurnAdmission(
            participant_id=request.participant_id,
            turn_id=request.turn_id,
            state=TurnAdmissionState.QUEUED,
            epoch=None,
            account_id=None,
            generation=None,
        )

    @staticmethod
    def _require_capacity(active_turn_count: int) -> None:
        if active_turn_count >= MAX_ACTIVE_TURNS_PER_PROVIDER:
            raise ParticipantRequestError(_ACTIVE_OPERATION_TIMEOUT)
