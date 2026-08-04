"""Load-bearing public journey for one coordinated Claude session."""

import socket
import threading

import pytest

from sidekick_usages.cli.session.claude.coordination import ClaudeCoordination
from sidekick_usages.cli.session.claude.host import ClaudeCliSession
from sidekick_usages.cli.session.claude.runtime import (
    ClaudeSessionGateError,
    ClaudeSessionRuntime,
    ClaudeTerminalEventsClosedError,
)
from sidekick_usages.cli.session.claude.terminal import (
    ClaudeTerminal,
    ClaudeTerminalSession,
    ClaudeTerminalState,
)
from sidekick_usages.core.selection.types import ParticipantId, SelectionCode
from sidekick_usages.providers.claude.structured.codec import (
    ClaudeProtectedChannelClosedError,
    ClaudeProtectedChannelError,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredDialogRequest,
    ClaudeStructuredElicitationRequest,
    ClaudeStructuredError,
    ClaudeStructuredHookCallbackRequest,
    ClaudeStructuredMcpMessageRequest,
    ClaudeStructuredPermissionDecision,
    ClaudeStructuredPermissionRequest,
    ClaudeStructuredQuestionAnswer,
    ClaudeStructuredQuestionRequest,
    ClaudeStructuredTerminalEvent,
)
from tests.fakes.claude.managed import StructuredResponseCase
from tests.fakes.claude.session import (
    SESSION_PARTICIPANT,
    SESSION_REQUESTS,
    SESSION_TURN,
    ClaudeSessionControlFake,
    ClaudeSessionEngineFake,
)

_CONVERSATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_SELECTION_PROMPT_INDEX = 2


class _JourneyTerminal(ClaudeTerminal):
    def __init__(
        self,
        control: ClaudeSessionControlFake,
        state: ClaudeTerminalState,
        turn_events_ready: threading.Event,
        events: list[str],
    ) -> None:
        self._control = control
        self._state = state
        self._turn_events_ready = turn_events_ready
        self._events = events
        self._prompt = 0
        self._interrupted = False
        self._cancelled: ClaudeStructuredPermissionRequest | None = None
        self._stream_correlation: str | None = None
        self.prompt_after_close: str | None = None
        self.rendered: list[str] = []

    def run(self, session: ClaudeTerminalSession) -> None:
        """Drive one compact retained-engine parity journey."""
        self._assert_recovered_state(session)
        self._turn_events_ready.set()
        while True:
            event = session.receive_event()
            try:
                self.render(event)
            except KeyboardInterrupt:
                session.interrupt()
            self._respond(session, event)
            if event.cancelled_request_id is not None:
                self._events.append(f"cancel:{event.cancelled_request_id}")
                if self._cancelled is None:
                    raise AssertionError("Missing cancelled request.")
                session.respond_permission(
                    self._cancelled,
                    ClaudeStructuredPermissionDecision.DENY,
                )
                self._cancelled = None
            if event.turn_complete:
                self._finish_recovered_turn(session)
                return

    def _assert_recovered_state(
        self,
        session: ClaudeTerminalSession,
    ) -> None:
        recovered = session.receive_event()
        if recovered.status != (
            "Sidekick recovered the terminal; Claude remained active."
        ):
            raise AssertionError("Terminal recovery status was lost.")
        self.render(recovered)
        if self._state.active_turn != SESSION_TURN:
            raise AssertionError("Active turn was lost during recreation.")
        if self._state.pending_prompt_count != 1:
            raise AssertionError("Queued prompt was lost or duplicated.")

    def _finish_recovered_turn(
        self,
        session: ClaudeTerminalSession,
    ) -> None:
        turn_id = self._state.begin_turn_completion()
        if self._state.active_turn != turn_id:
            raise AssertionError("Active turn cleared before supervisor end.")
        session.end_turn(turn_id)
        if not self._state.request_close():
            raise AssertionError("Active turn did not defer terminal close.")
        close = self._state.complete_turn(turn_id)
        if not close:
            raise AssertionError("Recovered turn lost terminal close intent.")
        self.prompt_after_close = self._state.claim_prompt(threading.Event())

    def _respond(
        self,
        session: ClaudeTerminalSession,
        event: ClaudeStructuredTerminalEvent,
    ) -> None:
        control = event.control
        if isinstance(control, ClaudeStructuredPermissionRequest):
            if control.request_id == "cancel-1":
                self._events.append("permission_pending")
                self._cancelled = control
                return
            decision = self.request_permission(control)
            try:
                session.respond_permission(control, decision)
            except ClaudeStructuredError:
                self._events.append("control_response_retry")
                session.respond_permission(control, decision)
        elif isinstance(control, ClaudeStructuredQuestionRequest):
            session.respond_question(control, self.request_question(control))
        elif isinstance(control, ClaudeStructuredElicitationRequest):
            session.decline_elicitation(control)
        elif isinstance(control, ClaudeStructuredDialogRequest):
            session.refuse_dialog(control)
        elif isinstance(
            control,
            ClaudeStructuredHookCallbackRequest
            | ClaudeStructuredMcpMessageRequest,
        ):
            message = (
                "Sidekick cannot run an undeclared Claude hook."
                if isinstance(control, ClaudeStructuredHookCallbackRequest)
                else ("Sidekick cannot route an undeclared SDK MCP server.")
            )
            self._events.append(f"presentation:{message}")
            session.refuse_unsupported_control(control)

    def read_prompt(self) -> str | None:
        self._prompt += 1
        if self._prompt == 1:
            self._control.refuse_once()
            return "refused prompt"
        if self._prompt == _SELECTION_PROMPT_INDEX:
            self._control.start_selection()
            return "queued prompt"
        return None

    def render(self, event: ClaudeStructuredTerminalEvent) -> None:
        if event.text_append and event.text_correlation is not None:
            if self._stream_correlation == event.text_correlation:
                self.rendered[-1] += "".join(event.text)
            else:
                self._stream_correlation = event.text_correlation
                self.rendered.extend(event.text)
        else:
            self.rendered.extend(event.text)
        if event.status is not None:
            self._events.append(f"presentation:{event.status}")
        if event.text_append and event.text and not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt

    def request_permission(
        self,
        request: ClaudeStructuredPermissionRequest,
    ) -> ClaudeStructuredPermissionDecision:
        if request.tool_name != "Bash":
            raise AssertionError("Unexpected permission request.")
        self._events.append("permission")
        return ClaudeStructuredPermissionDecision.DENY

    def request_question(
        self,
        request: ClaudeStructuredQuestionRequest,
    ) -> tuple[ClaudeStructuredQuestionAnswer, ...]:
        if request.questions[0].question != "Choose a mode":
            raise AssertionError("Unexpected Claude question request.")
        self._events.append("question")
        return (
            ClaudeStructuredQuestionAnswer(
                question="Choose a mode",
                answer="Safe",
                preview="readonly",
            ),
        )

    def render_status(self, code: SelectionCode) -> None:
        self._events.append(f"status:{code.value}")


class _FailingTerminal(ClaudeTerminal):
    def __init__(
        self,
        control: ClaudeSessionControlFake,
        events: list[str],
    ) -> None:
        self._control = control
        self._events = events

    def run(self, session: ClaudeTerminalSession) -> None:
        """Fail one terminal owner without touching its live session."""
        recovery = session.receive_event()
        if recovery.status != "Sidekick: selection_recovery_required":
            raise AssertionError("Ambiguous bootstrap was not gated.")
        with pytest.raises(ClaudeSessionGateError) as failure:
            session.start_turn("must remain behind bootstrap recovery")
        if failure.value.code is not SelectionCode.SELECTION_RECOVERY_REQUIRED:
            raise AssertionError("Ambiguous bootstrap used the wrong gate.")
        readiness = session.receive_event()
        if readiness.status != "Sidekick: selection_recovery_required":
            raise AssertionError("Ambiguous readiness was not gated.")
        self._control.retry_initial_readiness()
        for _index in range(3):
            session.receive_event()
        session.stop_terminal_events()
        self._events.append("terminal_failure")
        raise RuntimeError("synthetic terminal failure")


class _InFlightFailingTerminal(ClaudeTerminal):
    def __init__(
        self,
        control: ClaudeSessionControlFake,
        state: ClaudeTerminalState,
        events: list[str],
    ) -> None:
        self._control = control
        self._state = state
        self._events = events

    def run(self, session: ClaudeTerminalSession) -> None:
        """Fail one terminal while its provider turn remains active."""
        self._control.wait_initial_recovered()
        recovered = session.receive_event()
        if recovered.status != (
            "Sidekick recovered the terminal; Claude remained active."
        ):
            raise AssertionError("Bootstrap terminal recovery was lost.")
        self._events.append(f"presentation:{recovered.status}")
        self._control.refuse_once()
        with pytest.raises(ClaudeSessionGateError) as refusal:
            session.start_turn("refused prompt")
        self._events.append(f"status:{refusal.value.code.value}")
        self._control.start_selection()
        recovery = session.receive_event()
        if recovery.status != "Sidekick: selection_recovery_required":
            raise AssertionError("Ambiguous target install was not gated.")
        self._events.append(f"presentation:{recovery.status}")
        self._control.retry_target_projection()
        self._control.wait_target_recovered()
        self._state.queue_prompt("queued prompt")
        prompt = self._state.claim_prompt(threading.Event())
        if prompt != "queued prompt":
            raise AssertionError("Initial queued prompt text changed.")
        turn_id = session.start_turn(prompt)
        self._state.complete_start(turn_id)
        self._state.queue_prompt("follow-up prompt")
        self._control.disconnect()
        self._control.wait_reconnected()
        self._events.append("inflight_terminal_failure")
        raise RuntimeError("synthetic in-flight terminal failure")


def test_claude_session_keeps_one_engine_across_a_queued_switch() -> None:
    """Keep PID and conversation through refusal, switch, and interrupt."""
    events: list[str] = []
    state = ClaudeTerminalState()
    turn_events_ready = threading.Event()
    engine = ClaudeSessionEngineFake(
        (
            StructuredResponseCase.MALFORMED_UTF8,
            StructuredResponseCase.SUCCESS,
            StructuredResponseCase.MALFORMED_UTF8,
            StructuredResponseCase.SUCCESS,
            StructuredResponseCase.MALFORMED_UTF8,
            *(StructuredResponseCase.SUCCESS,) * 2,
        ),
        _stream_events(),
        events,
        expected_oauth_values=(
            "synthetic-oauth-a",
            "synthetic-oauth-a",
            "synthetic-oauth-b",
            "synthetic-oauth-b",
        ),
        fail_control_once=True,
        initial_event_count=3,
        turn_events_ready=turn_events_ready,
    )
    control = ClaudeSessionControlFake(events)
    control.recover_initial_projection_once()
    control.recover_target_projection_once()
    control.disconnect_initial_once()
    control.lose_end_ack_once()
    host, supervisor = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    runtime = ClaudeSessionRuntime(
        engine,
        control,
        host,
        supervisor,
        participant_id=SESSION_PARTICIPANT,
        coordination_factory=lambda participant_id, connection_generation: (
            _reconnect(control, participant_id, connection_generation)
        ),
        turn_id_factory=lambda: SESSION_TURN,
        request_id_factory=iter(SESSION_REQUESTS).__next__,
    )
    terminal = _JourneyTerminal(
        control,
        state,
        turn_events_ready,
        events,
    )
    terminals = iter(
        (
            _FailingTerminal(control, events),
            _InFlightFailingTerminal(control, state, events),
            terminal,
        )
    )

    status = ClaudeCliSession(
        runtime,
        terminals.__next__,
        lambda: events.append("profile_cleanup"),
    ).run(())

    cancelled = ClaudeTerminalState()
    cancelled.queue_prompt("cancelled prompt")
    if cancelled.claim_prompt(threading.Event()) != "cancelled prompt":
        raise AssertionError("Starting prompt was not claimed.")
    if not cancelled.request_close() or not cancelled.cancel_start():
        raise AssertionError("Cancelled start lost terminal close intent.")

    assert (
        status,
        engine.process_id == runtime.process_id,
        str(runtime.conversation_id),
        terminal.rendered,
        engine.user_turn_count,
        engine.event_consumer_count,
        state.active_turn,
        state.pending_prompt_count,
        terminal.prompt_after_close,
    ) == (
        0,
        True,
        _CONVERSATION,
        [
            "Suggestion: Check tests",
            "continued",
            "Claude could not complete the request.",
        ],
        1,
        1,
        None,
        1,
        None,
    )
    assert events == [
        "bootstrap_disconnect:1",
        "reattach:2",
        f"install:{SESSION_REQUESTS[0]}",
        f"install:{SESSION_REQUESTS[1]}",
        "recovery_receipt",
        "initialize",
        "initialize",
        "ready",
        "terminal_failure",
        (
            "presentation:Sidekick recovered the terminal; Claude remained "
            "active."
        ),
        f"status:{SelectionCode.SELECTION_RECOVERY_REQUIRED.value}",
        f"install:{SESSION_REQUESTS[4]}",
        "presentation:Sidekick: selection_recovery_required",
        f"install:{SESSION_REQUESTS[5]}",
        "receipt",
        "ready",
        "adoption",
        "prompt",
        "reattach:3",
        "inflight_terminal_failure",
        (
            "presentation:Sidekick recovered the terminal; Claude remained "
            "active."
        ),
        "presentation:A Claude hook is running.",
        "permission_pending",
        "cancel:cancel-1",
        "permission",
        "control_response_failed",
        "control_response_retry",
        "permission_response",
        "question",
        "question_response",
        "elicitation_response",
        "dialog_response",
        "presentation:Sidekick cannot run an undeclared Claude hook.",
        "hook_callback_response",
        "presentation:Sidekick cannot route an undeclared SDK MCP server.",
        "mcp_message_response",
        "presentation:Claude entered plan mode.",
        "presentation:Claude is thinking.",
        "presentation:Claude is thinking.",
        "presentation:Bash has been running for 2s.",
        "presentation:Claude usage limits are nearly exhausted.",
        "presentation:Claude authentication requires attention.",
        "interrupt",
        "end_applied",
        "reattach:4",
        "end_ack",
        "close_input",
        "wait",
        "profile_cleanup",
    ]


def test_claude_session_fails_closed_on_invalid_reattachment() -> None:
    """Close and surface one post-PREPARE safety failure exactly once."""
    events: list[str] = []
    engine = ClaudeSessionEngineFake(
        (StructuredResponseCase.SUCCESS,) * 2,
        (),
        events,
    )
    control = ClaudeSessionControlFake(events)
    host, supervisor = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    runtime = ClaudeSessionRuntime(
        engine,
        control,
        host,
        supervisor,
        participant_id=SESSION_PARTICIPANT,
        coordination_factory=lambda participant_id, connection_generation: (
            _reconnect(control, participant_id, connection_generation)
        ),
        request_id_factory=iter(SESSION_REQUESTS).__next__,
    )
    failure = ClaudeProtectedChannelError(
        "Synthetic invalid protected notice."
    )
    reporters_before = _binding_reporter_count()

    runtime.open()
    control.fail_registration_once(
        ClaudeProtectedChannelClosedError(
            "Synthetic protected registration closure."
        )
    )
    control.fail_notice_after_prepare_once(failure)
    control.disconnect()
    observed: BaseException | None = None
    trailing: BaseException | None = None
    closed = False
    try:
        recovery = runtime.receive_event()
        assert recovery.status == "Sidekick: selection_recovery_required"
        try:
            runtime.receive_event()
        except BaseException as error:
            observed = error
        control.wait_failed_attachment_closed()
        runtime.close()
        closed = True
        runtime.stop_terminal_events()
        try:
            runtime.receive_event()
        except BaseException as error:
            trailing = error
    finally:
        if not closed:
            runtime.close()

    assert observed is failure
    assert isinstance(trailing, ClaudeTerminalEventsClosedError)
    assert events == [
        f"install:{SESSION_REQUESTS[0]}",
        "initialize",
        "ready",
        "reattach:3",
        "fatal_notice:3",
    ]
    assert _binding_reporter_count() == reporters_before


def test_claude_session_gates_an_active_postcommit_projection() -> None:
    """Keep the old engine alive when projection races active work."""
    events: list[str] = []
    engine = ClaudeSessionEngineFake(
        (StructuredResponseCase.SUCCESS,) * 2,
        (),
        events,
    )
    control = ClaudeSessionControlFake(events)
    host, supervisor = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    runtime = ClaudeSessionRuntime(
        engine,
        control,
        host,
        supervisor,
        participant_id=SESSION_PARTICIPANT,
        turn_id_factory=lambda: SESSION_TURN,
        request_id_factory=iter(SESSION_REQUESTS).__next__,
    )
    runtime.open()
    turn_id = runtime.start_turn("keep this turn alive")
    control.start_selection(expect_receipt=False)
    degraded = runtime.receive_event()

    with pytest.raises(ClaudeSessionGateError) as failure:
        runtime.start_turn("must remain gated")
    assert (
        degraded.status,
        failure.value.code,
        engine.user_turn_count,
        engine.input_closed,
    ) == (
        "Sidekick: selection_recovery_required",
        SelectionCode.SELECTION_RECOVERY_REQUIRED,
        1,
        False,
    )
    runtime.end_turn(turn_id)
    assert runtime.finish_engine() == 0
    runtime.close()


def _reconnect(
    control: ClaudeSessionControlFake,
    participant_id: ParticipantId,
    connection_generation: int,
) -> ClaudeCoordination:
    if participant_id != SESSION_PARTICIPANT:
        raise AssertionError("Claude participant identity changed.")
    control.prepare_reconnect(connection_generation)
    host, supervisor = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return ClaudeCoordination(control, host, supervisor)


def _binding_reporter_count() -> int:
    return sum(
        thread.name == "claude-binding-report"
        for thread in threading.enumerate()
    )


def _stream_events() -> tuple[bytes, ...]:
    session = _CONVERSATION.encode()
    return (
        (
            b'{"type":"system","subtype":"task_notification",'
            b'"task_id":"unbookended-task"}'
        ),
        (
            b'{"type":"system","subtype":"session_state_changed",'
            b'"state":"idle","session_id":"' + session + b'"}'
        ),
        (b'{"type":"system","subtype":"background_tasks_changed","tasks":[]}'),
        b'{"type":"system","subtype":"init","session_id":"' + session + b'"}',
        (
            b'{"type":"system","subtype":"task_started",'
            b'"session_id":"'
            + session
            + b'","task_id":"agent-1","task_type":"local_agent"}'
        ),
        (
            b'{"type":"system","subtype":"hook_started",'
            b'"session_id":"' + session + b'","hook_id":"hook-1"}'
        ),
        (b'{"type":"system","subtype":"hook_progress","hook_id":"hook-1"}'),
        (b'{"type":"system","subtype":"hook_response","hook_id":"hook-1"}'),
        (
            b'{"type":"system","subtype":"task_updated",'
            b'"task_id":"agent-1","patch":{"status":"completed"}}'
        ),
        (
            b'{"type":"control_request","request_id":"cancel-1",'
            b'"request":{"subtype":"can_use_tool","tool_name":"Bash",'
            b'"tool_use_id":"tool-cancel","input":{"command":"sleep"}}}'
        ),
        b'{"type":"control_cancel_request","request_id":"cancel-1"}',
        (
            b'{"type":"control_request","request_id":"permission-1",'
            b'"request":{"subtype":"can_use_tool","tool_name":"Bash",'
            b'"tool_use_id":"tool-1","input":{"command":"true"},'
            b'"requires_user_interaction":true,'
            b'"permission_suggestions":[{"type":"addRules"}]}}'
        ),
        (
            b'{"type":"control_request","request_id":"question-1",'
            b'"request":{"subtype":"can_use_tool",'
            b'"tool_name":"AskUserQuestion","tool_use_id":"tool-2",'
            b'"input":{"questions":[{"question":"Choose a mode",'
            b'"header":"Mode","options":[{"label":"Safe",'
            b'"description":"Read only","preview":"readonly"},'
            b'{"label":"Fast","description":"Write enabled"}],'
            b'"multiSelect":false}]}}}'
        ),
        (
            b'{"type":"control_request","request_id":"elicitation-1",'
            b'"request":{"subtype":"elicitation",'
            b'"mcp_server_name":"server","message":"Approve?",'
            b'"mode":"form","url":"https://example.test",'
            b'"elicitation_id":"elicit-1","requested_schema":{},'
            b'"title":"Approval","display_name":"Server",'
            b'"description":"Provider request"}}'
        ),
        (
            b'{"type":"control_request","request_id":"dialog-1",'
            b'"request":{"subtype":"request_user_dialog",'
            b'"dialog_kind":"private","payload":{}}}'
        ),
        (
            b'{"type":"control_request","request_id":"hook-callback-1",'
            b'"request":{"subtype":"hook_callback",'
            b'"callback_id":"callback-1","input":{},'
            b'"tool_use_id":"tool-1"}}'
        ),
        (
            b'{"type":"control_request","request_id":"mcp-message-1",'
            b'"request":{"subtype":"mcp_message",'
            b'"server_name":"sdk-server","message":'
            b'{"jsonrpc":"2.0","id":"message-1",'
            b'"method":"tools/list"}}}'
        ),
        (
            b'{"type":"system","subtype":"status","status":null,'
            b'"permissionMode":"plan","uuid":"status-1",'
            b'"session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"system","subtype":"thinking_tokens",'
            b'"estimated_tokens":10,"estimated_tokens_delta":2,'
            b'"uuid":"thinking-1","session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"stream_event","session_id":"'
            + session
            + b'","event":{"type":"content_block_delta",'
            b'"index":0,"delta":{"type":"thinking_delta",'
            b'"thinking":"","estimated_tokens":0}}}'
        ),
        (
            b'{"type":"tool_progress","tool_use_id":"tool-1",'
            b'"tool_name":"Bash","parent_tool_use_id":null,'
            b'"elapsed_time_seconds":2,"uuid":"progress-1",'
            b'"session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"rate_limit_event","rate_limit_info":'
            b'{"status":"allowed_warning"},"uuid":"rate-1",'
            b'"session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"auth_status","isAuthenticating":false,'
            b'"output":[],"error":"provider secret",'
            b'"uuid":"auth-1","session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"prompt_suggestion","suggestion":"Check tests",'
            b'"uuid":"suggestion-1","session_id":"' + session + b'"}'
        ),
        (
            b'{"type":"stream_event","session_id":"'
            + session
            + b'","event":{"type":"message_start","message":'
            b'{"id":"message-1"}}}'
        ),
        (
            b'{"type":"stream_event","session_id":"'
            + session
            + b'","event":{"type":"content_block_delta",'
            b'"index":0,"delta":{"type":"text_delta",'
            b'"text":"con"}}}'
        ),
        (
            b'{"type":"stream_event","session_id":"'
            + session
            + b'","event":{"type":"content_block_delta",'
            b'"index":0,"delta":{"type":"text_delta",'
            b'"text":"tinued"}}}'
        ),
        b'{"type":"assistant","session_id":"'
        + session
        + b'","message":{"id":"message-1","role":"assistant","content":'
        b'[{"type":"text","text":"continued"}]}}',
        b'{"type":"result","subtype":"success","session_id":"'
        + session
        + b'","is_error":true,"errors":["provider secret"]}',
    )
