"""Protected supervisor behavior for one Claude session journey."""

import socket
from collections.abc import Iterator
from queue import Queue
from threading import Event, Thread, get_ident

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
    encode_protected_binding_query,
    encode_protected_projection,
    require_protected_binding_report,
    require_protected_install_receipt,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedHostChannel,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
)
from sidekick_usages.providers.claude.structured.process import (
    ClaudeStructuredProcess,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    encode_bounded_frame,
)
from sidekick_usages.serialization.json import (
    JsonObject,
    decode_json_object,
    encode_compact_json,
)
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
    RequestId("16161616-1616-4616-8616-161616161616"),
    RequestId("17171717-1717-4717-8717-171717171717"),
    RequestId("18181818-1818-4818-8818-181818181818"),
)
SESSION_OAUTH = ("synthetic-oauth-a", "synthetic-oauth-b")
_ACCOUNT_A = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_ACCOUNT_B = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_OPERATION_A = OperationId("44444444-4444-4444-8444-444444444444")
_OPERATION_B = OperationId("55555555-5555-4555-8555-555555555555")
_NONCE_A = RequestId("66666666-6666-4666-8666-666666666666")
_NONCE_B = RequestId("77777777-7777-4777-8777-777777777777")
_RECOVERY_NONCE = RequestId("15151515-1515-4515-8515-151515151515")
_TARGET_RECOVERY_NONCE = RequestId("19191919-1919-4919-8919-191919191919")
_QUERY_NONCE = RequestId("14141414-1414-4414-8414-141414141414")
_CONTROL_RESPONSE_SUBTYPES = {
    "dialog-1": "error",
    "hook-callback-1": "error",
    "mcp-message-1": "error",
}


def start_claude_binding_reporter(
    endpoint: socket.socket,
    participant_id: ParticipantId,
    connection_generation: int,
    binding: ClaudeStructuredBinding | None,
) -> None:
    """Serve one synchronous supervisor query from a fake host."""
    Thread(
        target=ClaudeProtectedHostChannel(
            endpoint, participant_id, connection_generation
        ).report_current_binding,
        args=(binding,),
        daemon=True,
    ).start()


class ClaudeSessionEngineFake(ClaudeStructuredEngineFake):
    """Extend the common engine fake with one interactive journey."""

    def __init__(
        self,
        responses: tuple[StructuredResponseCase, ...],
        interactive_events: tuple[bytes, ...],
        journey_events: list[str],
        *,
        expected_oauth_values: tuple[str, ...] = SESSION_OAUTH,
        fail_control_once: bool = False,
        initial_event_count: int = 0,
        turn_events_ready: Event | None = None,
    ) -> None:
        super().__init__(responses, expected_oauth_values)
        self._interactive_events = list(interactive_events)
        self._journey_events = journey_events
        self._fail_control_once = fail_control_once
        self._initial_event_count = initial_event_count
        self._turn_events_ready = turn_events_ready
        self._event_consumers: set[int] = set()
        self.interactive_frames: list[JsonObject] = []

    @property
    def event_consumer_count(self) -> int:
        """Return the number of engine-lifetime event consumers."""
        return len(self._event_consumers)

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
            if response_case is StructuredResponseCase.SUCCESS:
                encoded = encode_compact_json(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": str(request_id),
                            "response": {"commands": []},
                        },
                    }
                )
                return _route_control_response(encoded, request_id)
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
                "hook-callback-1": "hook_callback_response",
                "mcp-message-1": "mcp_message_response",
            }
            label = labels.get(request_id)
            if label is None:
                raise AssertionError("Unexpected Claude control response.")
            expected_subtype = _CONTROL_RESPONSE_SUBTYPES.get(
                request_id,
                "success",
            )
            assert response.get("subtype") == expected_subtype, (
                "Unsupported control did not fail closed."
            )
            self._fail_control_if_requested()
            if request_id == "question-1":
                payload = response.get("response")
                if not isinstance(payload, dict):
                    raise AssertionError("Missing Claude question response.")
                updated = payload.get("updatedInput")
                if not isinstance(updated, dict) or updated.get("answers") != {
                    "Choose a mode": "Safe"
                }:
                    raise AssertionError("Claude question answers were lost.")
            payload = response.get("response")
            if isinstance(payload, dict) and "updatedPermissions" in payload:
                raise AssertionError("Permission suggestions were implicit.")
            self._journey_events.append(label)

    def _fail_control_if_requested(self) -> None:
        if not self._fail_control_once:
            return
        self._fail_control_once = False
        self._journey_events.append("control_response_failed")
        raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_TIMEOUT)

    def receive_event(self, timeout_seconds: float) -> bytes:
        """Return available events or one typed polling boundary."""
        del timeout_seconds
        self._event_consumers.add(get_ident())
        if self._initial_event_count:
            self._initial_event_count -= 1
            return self._interactive_events.pop(0)
        if (
            self._turn_events_ready is not None
            and not self._turn_events_ready.is_set()
        ):
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROTOCOL_TIMEOUT
            )
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
        self._selection_started = False
        self._target_open = False
        self._connection_generation = 1
        self._disconnect_initial = False
        self._recover_initial = False
        self._recover_target = False
        self._target_recovery_sent = False
        self._recovered_initial = False
        self._initial_projected = False
        self._initial_receipt_proven = False
        self._initial_ready = False
        self._registration_failure: BaseException | None = None
        self._notice_failure: BaseException | None = None
        self._notice_failure_raised = False
        self._failed_attachment_closed = Event()
        self._initial_recovered = Event()
        self._target_recovered = Event()
        self._reconnected = Event()
        self._reopen_applied = Event()
        self._initial = _binding(_OPERATION_A, _ACCOUNT_A, SESSION_OAUTH[0], 1)
        self._target = _binding(_OPERATION_B, _ACCOUNT_B, SESSION_OAUTH[1], 2)

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register and project the exact baseline authority."""
        failure = self._registration_failure
        if failure is not None:
            self._registration_failure = None
            raise failure
        if manifest.participant_id != SESSION_PARTICIPANT:
            raise AssertionError("Unexpected Claude participant.")
        if manifest.connection_generation != self._connection_generation:
            raise AssertionError("Unexpected Claude connection generation.")
        self._endpoint = protected_endpoint
        expected = (
            None
            if not self._initial_projected
            else (self._target if self._target_open else self._initial)
        )
        if _query(protected_endpoint, self._connection_generation) != expected:
            raise AssertionError("Claude binding report did not match.")
        if self._disconnect_initial:
            self._disconnect_initial = False
            self._events.append(
                f"bootstrap_disconnect:{self._connection_generation}"
            )
            protected_endpoint.close()
            self._endpoint = None
        elif not self._initial_projected:
            _send(
                protected_endpoint,
                self._initial,
                SESSION_OAUTH[0],
                _NONCE_A,
                self._connection_generation,
            )
            self._initial_projected = True
        if self._connection_generation > 1:
            self._events.append(f"reattach:{self._connection_generation}")
            self._reconnected.set()
        return ParticipantRegistration(
            participant_id=SESSION_PARTICIPANT,
            provider_id=ProviderId.CLAUDE,
            connection_generation=self._connection_generation,
            registered_epoch=SelectionEpoch(1),
            pending_epoch=None,
        )

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield the baseline and exact transition notices."""
        notices = self._notices
        yield from self._initial_notices()
        failure = self._notice_failure
        if failure is not None:
            self._notice_failure = None
            yield _notice(ParticipantNoticeKind.PREPARE, 1)
            self._events.append(f"fatal_notice:{self._connection_generation}")
            self._notice_failure_raised = True
            raise failure
        yield from self._open_notices()
        while (notice := notices.get()) is not None:
            yield notice
            if notice.kind is ParticipantNoticeKind.OPEN:
                if self._target_open:
                    self._target_recovered.set()
                else:
                    self._initial_recovered.set()
            elif notice.kind is ParticipantNoticeKind.STATUS:
                self._status_applied.set()
            elif notice.kind is ParticipantNoticeKind.PREPARE:
                self._prepare_applied.set()

    def _initial_notices(self) -> Iterator[ParticipantNotice]:
        if self._recover_initial:
            self._recover_initial = False
            yield _notice(ParticipantNoticeKind.PREPARE, 1)
            self._recover_initial_authority()
        elif not self._initial_receipt_proven:
            self._prove_initial_receipt()
        if not self._initial_ready:
            yield ParticipantNotice(
                participant_id=SESSION_PARTICIPANT,
                provider_id=ProviderId.CLAUDE,
                kind=ParticipantNoticeKind.READY,
                epoch=self._initial.epoch,
                operation_id=self._initial.operation_id,
                target_account_id=self._initial.account_id,
                target_generation=self._initial.generation,
            )

    def _open_notices(self) -> Iterator[ParticipantNotice]:
        if self._initial_ready:
            yield _notice(
                ParticipantNoticeKind.OPEN,
                2 if self._target_open else 1,
            )
            self._reopen_applied.set()

    def disconnect(self) -> None:
        """Drop only the current fake supervisor attachment."""
        self._reconnected.clear()
        self._reopen_applied.clear()
        self._notices.put(None)
        if self._endpoint is not None:
            self._endpoint.close()
            self._endpoint = None

    def disconnect_initial_once(self) -> None:
        """Drop generation one before projecting its first binding."""
        self._disconnect_initial = True

    def recover_initial_projection_once(self) -> None:
        """Replace one ambiguous bootstrap lease with a fresh projection."""
        self._recover_initial = True

    def wait_initial_recovered(self) -> None:
        """Wait until fresh bootstrap recovery reopens the initial epoch."""
        if not self._initial_recovered.wait(timeout=2):
            raise AssertionError("Claude bootstrap recovery did not open.")

    def retry_initial_readiness(self) -> None:
        """Retry readiness without projecting the proven authority again."""
        self._notices.put(
            ParticipantNotice(
                participant_id=SESSION_PARTICIPANT,
                provider_id=ProviderId.CLAUDE,
                kind=ParticipantNoticeKind.READY,
                epoch=self._initial.epoch,
                operation_id=self._initial.operation_id,
                target_account_id=self._initial.account_id,
                target_generation=self._initial.generation,
            )
        )

    def recover_target_projection_once(self) -> None:
        """Send one fresh lease after an ambiguous target install."""
        self._recover_target = True

    def retry_target_projection(self) -> None:
        """Project a fresh target lease after the ambiguous attempt."""
        if not self._recover_target:
            raise AssertionError("Target recovery was not requested.")
        self._recover_target = False
        self._target_recovery_sent = True
        _send(
            self._require_endpoint(),
            self._target,
            SESSION_OAUTH[1],
            _TARGET_RECOVERY_NONCE,
            self._connection_generation,
        )
        self._receipt = Thread(target=self._finish_install, daemon=True)
        self._receipt.start()

    def wait_target_recovered(self) -> None:
        """Wait until the fresh target projection reopens admission."""
        if not self._target_recovered.wait(timeout=2):
            raise AssertionError("Claude target recovery did not open.")

    def fail_registration_once(self, failure: BaseException) -> None:
        """Fail one attachment before its binding reporter can finish."""
        self._registration_failure = failure

    def fail_notice_after_prepare_once(self, failure: BaseException) -> None:
        """Fail one attachment after its first valid PREPARE notice."""
        self._notice_failure = failure

    def prepare_reconnect(self, connection_generation: int) -> None:
        """Prepare one strictly newer fake supervisor attachment."""
        self._connection_generation = connection_generation
        self._notices = Queue()

    def wait_reconnected(self) -> None:
        """Wait for the exact binding re-registration proof."""
        if not self._reconnected.wait(timeout=2):
            raise AssertionError("Claude participant did not reattach.")
        if not self._reopen_applied.wait(timeout=2):
            raise AssertionError("Claude participant did not reopen.")

    def wait_failed_attachment_closed(self) -> None:
        """Wait until the fatally invalid attachment is released."""
        if not self._failed_attachment_closed.wait(timeout=2):
            raise AssertionError("Fatal Claude attachment remained open.")

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

    def start_selection(self, *, expect_receipt: bool = True) -> None:
        """Project the target only after PREPARE."""
        self._selection_started = True
        endpoint = self._require_endpoint()
        if not self._initial_receipt_proven:
            self._prove_initial_receipt()
        self._notices.put(_notice(ParticipantNoticeKind.PREPARE, 2))
        if not self._prepare_applied.wait(timeout=2):
            raise AssertionError("Claude preparation was not applied.")
        _send(
            endpoint,
            self._target,
            SESSION_OAUTH[1],
            _NONCE_B,
            self._connection_generation,
        )
        if expect_receipt and not self._recover_target:
            self._receipt = Thread(target=self._finish_install, daemon=True)
            self._receipt.start()

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Queue until target installation and OPEN are complete."""
        if turn_id != SESSION_TURN:
            raise AssertionError("Unexpected Claude turn.")
        binding = self._target if self._selection_started else self._initial
        admitted = not self._selection_started or self._target_open
        return TurnAdmission(
            participant_id=SESSION_PARTICIPANT,
            turn_id=turn_id,
            state=(
                TurnAdmissionState.ADMITTED
                if admitted
                else TurnAdmissionState.QUEUED
            ),
            epoch=binding.epoch if admitted else None,
            account_id=binding.account_id if admitted else None,
            generation=binding.generation if admitted else None,
        )

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Open only the exactly installed target."""
        binding = self._target if self._selection_started else self._initial
        if proof != ParticipantReadyProof(
            account_id=binding.account_id,
            generation=binding.generation,
            epoch=binding.epoch,
        ):
            raise AssertionError("Unexpected Claude readiness proof.")
        self._events.append("ready")
        if self._selection_started:
            self._target_open = True
        else:
            self._initial_ready = True
        self._notices.put(
            _notice(ParticipantNoticeKind.OPEN, binding.epoch.value)
        )

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Record adoption before transmission."""
        binding = self._target if self._selection_started else self._initial
        if proof != ParticipantAdoptionProof(
            turn_id=SESSION_TURN,
            account_id=binding.account_id,
            generation=binding.generation,
            epoch=binding.epoch,
        ):
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
        if self._notice_failure_raised:
            self._failed_attachment_closed.set()
        if self._receipt is not None:
            self._receipt.join(timeout=2)
        if self._endpoint is not None:
            self._endpoint.close()

    def _finish_install(self) -> None:
        recovered = self._target_recovery_sent
        nonce = _TARGET_RECOVERY_NONCE if recovered else _NONCE_B
        request_index = 4 if self._recovered_initial else 2
        if recovered:
            request_index += 1
        _receive_receipt(
            self._require_endpoint(),
            self._target,
            nonce,
            self._connection_generation,
            SESSION_REQUESTS[request_index],
        )
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

    def _recover_initial_authority(self) -> None:
        endpoint = self._require_endpoint()
        _send(
            endpoint,
            self._initial,
            SESSION_OAUTH[0],
            _RECOVERY_NONCE,
            self._connection_generation,
        )
        _receive_receipt(
            endpoint,
            self._initial,
            _RECOVERY_NONCE,
            self._connection_generation,
            SESSION_REQUESTS[1],
        )
        self._events.append("recovery_receipt")
        self._initial_receipt_proven = True
        self._recovered_initial = True

    def _prove_initial_receipt(self) -> None:
        _receive_receipt(
            self._require_endpoint(),
            self._initial,
            _NONCE_A,
            self._connection_generation,
            SESSION_REQUESTS[0],
        )
        self._initial_receipt_proven = True

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
    connection_generation: int,
) -> None:
    payload = encode_protected_projection(
        binding,
        bytearray(oauth, "utf-8"),
        nonce,
        participant_id=SESSION_PARTICIPANT,
        connection_generation=connection_generation,
    )
    endpoint.sendall(
        encode_bounded_frame(payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    )
    clear_secret_buffer(payload)


def _query(
    endpoint: socket.socket,
    connection_generation: int,
) -> ClaudeStructuredBinding | None:
    payload = encode_protected_binding_query(
        _QUERY_NONCE,
        SESSION_PARTICIPANT,
        connection_generation,
    )
    endpoint.sendall(
        encode_bounded_frame(payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    )
    decoder = BoundedFrameDecoder(MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    while True:
        chunk = endpoint.recv(64 * 1024)
        if not chunk:
            raise AssertionError("Protected endpoint closed before report.")
        reports = decoder.feed(chunk)
        if reports:
            return require_protected_binding_report(
                reports[0],
                _QUERY_NONCE,
                SESSION_PARTICIPANT,
                connection_generation,
            )


def _receive_receipt(
    endpoint: socket.socket,
    binding: ClaudeStructuredBinding,
    nonce: RequestId,
    connection_generation: int,
    structured_request_id: RequestId,
) -> None:
    decoder = BoundedFrameDecoder(MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    while True:
        chunk = endpoint.recv(64 * 1024)
        if not chunk:
            raise AssertionError("Protected endpoint closed before receipt.")
        if frames := decoder.feed(chunk):
            if len(frames) != 1 or decoder.pending:
                raise AssertionError(
                    "Protected install receipt was malformed."
                )
            receipt = require_protected_install_receipt(
                frames[0],
                binding,
                nonce,
                SESSION_PARTICIPANT,
                connection_generation,
            )
            if receipt.request_id != structured_request_id:
                raise AssertionError("Structured install receipt mismatched.")
            return


def _route_control_response(
    encoded: bytes,
    request_id: RequestId,
) -> bytes:
    transport = ClaudeStructuredProcess.__new__(ClaudeStructuredProcess)
    transport._buffer = bytearray(encoded + b"\n")
    transport._event_frames = []
    transport._event_bytes = 0
    response = transport._consume_pending_frames(request_id)
    if response != encoded:
        raise AssertionError("Initialize response routing changed.")
    return response
