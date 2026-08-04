"""Authenticated provider-neutral participant control consumption."""

import socket
from collections.abc import Iterator
from threading import RLock
from typing import NoReturn

from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    ControlEvent,
    FailedPayload,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantReadyProof,
    ParticipantRegistration,
    TurnAdmission,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
)


class SessionParticipantError(RuntimeError):
    """One authenticated participant failure carrying a safe code."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class ParticipantControl:
    """Consume one participant's correlated supervisor control streams."""

    def __init__(
        self,
        action_client: ControlClient,
        subscription_client: ControlClient,
        manifest: ParticipantManifest,
    ) -> None:
        self._action_client = action_client
        self._subscription_client = subscription_client
        self._manifest = manifest
        self._lock = RLock()

    @property
    def participant_id(self) -> ParticipantId:
        """Return the stable registered participant identifier."""
        return self._manifest.participant_id

    def register(
        self,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register this exact process with one protected endpoint."""
        payload = self._payload(
            self._action_client.register_participant(
                self._manifest,
                protected_endpoint=protected_endpoint,
            ),
            EventKind.PARTICIPANT_REGISTERED,
        )
        if (
            not isinstance(payload, ParticipantRegistration)
            or payload.participant_id != self._manifest.participant_id
            or payload.provider_id is not self._manifest.provider_id
            or payload.connection_generation
            != self._manifest.connection_generation
        ):
            self._fail(SelectionCode.AUTHORITY_PROOF_FAILED)
        return payload

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield only exact authenticated notices after acceptance."""
        events = self._subscription_client.subscribe_participant(
            self._manifest.participant_id,
            self._manifest.connection_generation,
        )
        try:
            accepted = next(events)
        except StopIteration:
            self._fail(SelectionCode.SELECTION_RECOVERY_REQUIRED)
        if (
            accepted.kind is not EventKind.ACCEPTED
            or not isinstance(accepted.payload, AcceptedPayload)
            or accepted.payload.operation_id is not None
        ):
            self._fail(SelectionCode.SELECTION_RECOVERY_REQUIRED)
        for event in events:
            notice = event.payload
            if (
                event.kind is not EventKind.PARTICIPANT_NOTICE
                or not isinstance(notice, ParticipantNotice)
                or notice.participant_id != self._manifest.participant_id
                or notice.provider_id is not self._manifest.provider_id
            ):
                self._fail(SelectionCode.AUTHORITY_PROOF_FAILED)
            yield notice

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Return one exact admitted or queued turn boundary."""
        with self._lock:
            payload = self._payload(
                self._action_client.begin_turn(
                    self._manifest.participant_id,
                    self._manifest.connection_generation,
                    turn_id,
                ),
                EventKind.TURN_ADMISSION,
            )
        if (
            not isinstance(payload, TurnAdmission)
            or payload.participant_id != self._manifest.participant_id
            or payload.turn_id != turn_id
        ):
            self._fail(SelectionCode.AUTHORITY_PROOF_FAILED)
        return payload

    def end(self, turn_id: TurnId) -> None:
        """Close one naturally terminal exact turn lease."""
        with self._lock:
            events = self._action_client.end_turn(
                self._manifest.participant_id,
                self._manifest.connection_generation,
                turn_id,
            )
            self._completed(events)

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Publish readiness after provider-local installation."""
        with self._lock:
            self._completed(
                self._action_client.participant_ready(
                    self._manifest.participant_id,
                    self._manifest.connection_generation,
                    proof,
                )
            )

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Publish first-real-turn adoption before transmission."""
        with self._lock:
            self._completed(
                self._action_client.participant_adopted(
                    self._manifest.participant_id,
                    self._manifest.connection_generation,
                    proof,
                )
            )

    def close(self) -> None:
        """Close both control connections owned by this participant."""
        failures: list[BaseException] = []
        for client in (self._subscription_client, self._action_client):
            try:
                client.close()
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup(
                "Participant control connections did not both close.",
                failures,
            )

    def _completed(self, events: Iterator[ControlEvent]) -> None:
        payload = self._payload(events, EventKind.COMPLETED)
        if (
            not isinstance(payload, CompletedPayload)
            or payload.operation_id is not None
            or payload.outcome is not CompletionOutcome.SUCCEEDED
        ):
            self._fail(SelectionCode.AUTHORITY_PROOF_FAILED)

    @staticmethod
    def _payload(
        events: Iterator[ControlEvent],
        expected_kind: EventKind,
    ) -> object:
        try:
            event = next(events)
        except StopIteration:
            return ParticipantControl._fail(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        if event.kind is EventKind.FAILED:
            payload = event.payload
            if isinstance(payload, FailedPayload):
                code = safe_outcome_code(payload.code)
                try:
                    selection_code = (
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                        if code is None
                        else SelectionCode(code)
                    )
                except ValueError:
                    selection_code = SelectionCode.SELECTION_RECOVERY_REQUIRED
                ParticipantControl._fail(selection_code)
        if event.kind is not expected_kind:
            ParticipantControl._fail(SelectionCode.SELECTION_RECOVERY_REQUIRED)
        try:
            next(events)
        except StopIteration:
            return event.payload
        return ParticipantControl._fail(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
        )

    @staticmethod
    def _fail(code: SelectionCode) -> NoReturn:
        raise SessionParticipantError(code)
