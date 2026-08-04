"""Protected supervisor behavior for one Claude session journey."""

import socket
from collections.abc import Iterator
from queue import Queue
from threading import Event, Thread

from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantRegistration,
    TurnAdmission,
    TurnAdmissionState,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.structured.codec import (
    MAX_CLAUDE_PROTECTED_FRAME_BYTES,
    clear_secret_buffer,
    encode_protected_projection,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    encode_bounded_frame,
)
from sidekick_usages.serialization.json import JsonObject, decode_json_object
from tests.fakes.claude.managed import (
    ClaudeStructuredEngineFake,
    StructuredResponseCase,
)

SESSION_PARTICIPANT = ParticipantId("11111111-1111-4111-8111-111111111111")
SESSION_TURN = TurnId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SESSION_REQUESTS = (
    RequestId("88888888-8888-4888-8888-888888888888"),
    RequestId("99999999-9999-4999-8999-999999999999"),
    RequestId("12121212-1212-4212-8212-121212121212"),
    RequestId("13131313-1313-4313-8313-131313131313"),
)
SESSION_OAUTH = ("synthetic-oauth-a", "synthetic-oauth-b")
_ACCOUNT_A = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_ACCOUNT_B = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_OPERATION_A = OperationId("44444444-4444-4444-8444-444444444444")
_OPERATION_B = OperationId("55555555-5555-4555-8555-555555555555")
_NONCE_A = RequestId("66666666-6666-4666-8666-666666666666")
_NONCE_B = RequestId("77777777-7777-4777-8777-777777777777")


class ClaudeSessionEngineFake(ClaudeStructuredEngineFake):
    """Extend the common engine fake with one interactive journey."""

    def __init__(
        self,
        responses: tuple[StructuredResponseCase, ...],
        interactive_events: tuple[bytes, ...],
        journey_events: list[str],
    ) -> None:
        super().__init__(responses, SESSION_OAUTH)
        self._interactive_events = list(interactive_events)
        self._journey_events = journey_events
        self.interactive_frames: list[JsonObject] = []

    def exchange(
        self,
        request: bytearray,
        request_id: RequestId,
        timeout_seconds: float,
    ) -> bytes:
        """Handle interrupt or delegate an OAuth install."""
        root = decode_json_object(request[:-1])
        if root.get("type") != "control_request":
            response = super().exchange(
                request,
                request_id,
                timeout_seconds,
            )
            self._journey_events.append(f"install:{request_id}")
            return response
        control = root.get("request")
        if not isinstance(control, dict):
            raise AssertionError("Unexpected structured control request.")
        if control.get("subtype") == "initialize":
            if control.get("supportedDialogKinds") != []:
                raise AssertionError("Claude dialog kinds were not disabled.")
            response_case = self._responses.pop(0)
            clear_secret_buffer(request)
            self._journey_events.append("initialize")
            return self._response(response_case, str(request_id))
        if control.get("subtype") != "interrupt":
            raise AssertionError("Unexpected structured control request.")
        response_case = self._responses.pop(0)
        clear_secret_buffer(request)
        self._journey_events.append("interrupt")
        return self._response(response_case, str(request_id))

    def send_interactive(
        self,
        frame: bytearray,
        timeout_seconds: float,
    ) -> None:
        """Record one prompt or correlated permission response."""
        del timeout_seconds
        root = decode_json_object(frame[:-1])
        self.interactive_frames.append(root)
        clear_secret_buffer(frame)
        frame_type = root.get("type")
        if frame_type == "user":
            self.user_turn_count += 1
            self._journey_events.append("prompt")
        elif frame_type == "control_response":
            response = root.get("response")
            if not isinstance(response, dict):
                raise AssertionError("Missing Claude control response.")
            request_id = response.get("request_id")
            if not isinstance(request_id, str):
                raise AssertionError("Invalid Claude control request ID.")
            labels = {
                "permission-1": "permission_response",
                "question-1": "question_response",
                "elicitation-1": "elicitation_response",
                "dialog-1": "dialog_response",
            }
            label = labels.get(request_id)
            if label is None:
                raise AssertionError("Unexpected Claude control response.")
            if request_id == "question-1":
                payload = response.get("response")
                if not isinstance(payload, dict):
                    raise AssertionError("Missing Claude question response.")
                updated = payload.get("updatedInput")
                if not isinstance(updated, dict) or updated.get("answers") != {
                    "Choose a mode": "Safe"
                }:
                    raise AssertionError("Claude question answers were lost.")
            self._journey_events.append(label)

    def receive_event(self, timeout_seconds: float) -> bytes:
        """Return available events or one typed polling boundary."""
        del timeout_seconds
        if not self.user_turn_count or not self._interactive_events:
            failure = (
                ClaudeStructuredFailure.PROTOCOL_EOF
                if self.input_closed
                else ClaudeStructuredFailure.PROTOCOL_TIMEOUT
            )
            raise ClaudeStructuredError(failure)
        return self._interactive_events.pop(0)

    def close_input(self) -> None:
        """Record natural input closure."""
        super().close_input()
        self._journey_events.append("close_input")

    def wait(self, timeout_seconds: float) -> int:
        """Record one ordinary engine exit."""
        status = super().wait(timeout_seconds)
        self._journey_events.append("wait")
        return status


class ClaudeSessionControlFake:
    """Drive refusal, protected install, readiness, and adoption."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._notices: Queue[ParticipantNotice | None] = Queue()
        self._endpoint: socket.socket | None = None
        self._receipt: Thread | None = None
        self._prepare_applied = Event()
        self._status_applied = Event()
        self._target_open = False
        self._initial = _binding(_OPERATION_A, _ACCOUNT_A, SESSION_OAUTH[0], 1)
        self._target = _binding(_OPERATION_B, _ACCOUNT_B, SESSION_OAUTH[1], 2)

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register and project the exact baseline authority."""
        if manifest.participant_id != SESSION_PARTICIPANT:
            raise AssertionError("Unexpected Claude participant.")
        self._endpoint = protected_endpoint
        _send(protected_endpoint, self._initial, SESSION_OAUTH[0], _NONCE_A)
        return ParticipantRegistration(
            participant_id=SESSION_PARTICIPANT,
            provider_id=ProviderId.CLAUDE,
            connection_generation=1,
            registered_epoch=SelectionEpoch(1),
            pending_epoch=None,
        )

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield the baseline and exact transition notices."""
        yield _notice(ParticipantNoticeKind.OPEN, 1)
        while (notice := self._notices.get()) is not None:
            yield notice
            if notice.kind is ParticipantNoticeKind.STATUS:
                self._status_applied.set()
            elif notice.kind is ParticipantNoticeKind.PREPARE:
                self._prepare_applied.set()

    def refuse_once(self) -> None:
        """Publish a recoverable precommit refusal for the live engine."""
        self._notices.put(
            ParticipantNotice(
                participant_id=SESSION_PARTICIPANT,
                provider_id=ProviderId.CLAUDE,
                kind=ParticipantNoticeKind.STATUS,
                epoch=SelectionEpoch(1),
                code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )
        )
        if not self._status_applied.wait(timeout=2):
            raise AssertionError("Claude refusal was not applied.")

    def start_selection(self) -> None:
        """Project the target only after PREPARE."""
        endpoint = self._require_endpoint()
        _receive(endpoint)
        self._notices.put(_notice(ParticipantNoticeKind.PREPARE, 2))
        if not self._prepare_applied.wait(timeout=2):
            raise AssertionError("Claude preparation was not applied.")
        _send(endpoint, self._target, SESSION_OAUTH[1], _NONCE_B)
        self._receipt = Thread(target=self._finish_install, daemon=True)
        self._receipt.start()

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Queue until target installation and OPEN are complete."""
        if turn_id != SESSION_TURN:
            raise AssertionError("Unexpected Claude turn.")
        return TurnAdmission(
            participant_id=SESSION_PARTICIPANT,
            turn_id=turn_id,
            state=(
                TurnAdmissionState.ADMITTED
                if self._target_open
                else TurnAdmissionState.QUEUED
            ),
            epoch=self._target.epoch if self._target_open else None,
            account_id=(
                self._target.account_id if self._target_open else None
            ),
            generation=(
                self._target.generation if self._target_open else None
            ),
        )

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Open only the exactly installed target."""
        if proof.epoch != self._target.epoch:
            raise AssertionError("Unexpected Claude readiness proof.")
        self._events.append("ready")
        self._target_open = True
        self._notices.put(_notice(ParticipantNoticeKind.OPEN, 2))

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Record adoption before transmission."""
        if proof.turn_id != SESSION_TURN:
            raise AssertionError("Unexpected Claude adoption proof.")
        self._events.append("adoption")

    def end(self, turn_id: TurnId) -> None:
        """Record the naturally terminal exact turn."""
        if turn_id != SESSION_TURN:
            raise AssertionError("Unexpected Claude turn completion.")
        self._events.append("end")

    def close(self) -> None:
        """Release only fake control resources."""
        self._notices.put(None)
        if self._receipt is not None:
            self._receipt.join(timeout=2)
        if self._endpoint is not None:
            self._endpoint.close()

    def _finish_install(self) -> None:
        _receive(self._require_endpoint())
        self._events.append("receipt")
        self._notices.put(
            ParticipantNotice(
                participant_id=SESSION_PARTICIPANT,
                provider_id=ProviderId.CLAUDE,
                kind=ParticipantNoticeKind.READY,
                epoch=self._target.epoch,
                operation_id=self._target.operation_id,
                target_account_id=self._target.account_id,
                target_generation=self._target.generation,
            )
        )

    def _require_endpoint(self) -> socket.socket:
        if self._endpoint is None:
            raise AssertionError("Protected endpoint was not registered.")
        return self._endpoint


def _binding(
    operation: OperationId,
    account: SidekickAccountId,
    oauth: str,
    epoch: int,
) -> ClaudeStructuredBinding:
    return ClaudeStructuredBinding(
        operation_id=operation,
        account_id=account,
        generation=claude_access_token_generation(oauth),
        epoch=SelectionEpoch(epoch),
    )


def _notice(kind: ParticipantNoticeKind, epoch: int) -> ParticipantNotice:
    return ParticipantNotice(
        participant_id=SESSION_PARTICIPANT,
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        epoch=SelectionEpoch(epoch),
    )


def _send(
    endpoint: socket.socket,
    binding: ClaudeStructuredBinding,
    oauth: str,
    nonce: RequestId,
) -> None:
    payload = encode_protected_projection(
        binding,
        bytearray(oauth, "utf-8"),
        nonce,
        participant_id=SESSION_PARTICIPANT,
        connection_generation=1,
    )
    endpoint.sendall(
        encode_bounded_frame(payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    )
    clear_secret_buffer(payload)


def _receive(endpoint: socket.socket) -> None:
    decoder = BoundedFrameDecoder(MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    while True:
        chunk = endpoint.recv(64 * 1024)
        if not chunk:
            raise AssertionError("Protected endpoint closed before receipt.")
        if decoder.feed(chunk):
            return
