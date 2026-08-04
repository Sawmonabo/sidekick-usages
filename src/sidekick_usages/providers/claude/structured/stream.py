"""Strict user and terminal frames for one structured Claude engine."""

from typing import NoReturn

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.accounts.validation import (
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.process import (
    MAX_CLAUDE_CONTROL_FRAME_BYTES,
)
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredActivityKind,
    ClaudeStructuredActivityState,
    ClaudeStructuredControlRequest,
    ClaudeStructuredConversationId,
    ClaudeStructuredDialogRequest,
    ClaudeStructuredElicitationRequest,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredPermissionDecision,
    ClaudeStructuredPermissionRequest,
    ClaudeStructuredQuestion,
    ClaudeStructuredQuestionAnswer,
    ClaudeStructuredQuestionOption,
    ClaudeStructuredQuestionRequest,
    ClaudeStructuredStreamEvent,
    ClaudeStructuredTerminalEvent,
)
from sidekick_usages.serialization.json import (
    JsonObject,
    JsonValue,
    decode_json_object,
    encode_compact_json_buffer,
)

_MESSAGE_TYPES = frozenset(
    {
        "assistant",
        "auth_status",
        "control_cancel_request",
        "control_request",
        "prompt_suggestion",
        "rate_limit_event",
        "result",
        "stream_event",
        "system",
        "tool_progress",
        "tool_use_summary",
        "user",
    }
)
_PERMISSION_REQUIRED_KEYS = frozenset(
    {"subtype", "tool_name", "input", "tool_use_id"}
)
_QUESTION_INPUT_KEYS = frozenset({"questions", "afkTimeoutMs"})
_QUESTION_KEYS = frozenset({"question", "header", "options", "multiSelect"})
_QUESTION_OPTION_KEYS = frozenset({"label", "description", "preview"})
_PERMISSION_OPTIONAL_KEYS = frozenset(
    {
        "agent_id",
        "blocked_path",
        "decision_reason",
        "description",
        "display_name",
        "permission_suggestions",
        "requires_user_interaction",
        "title",
    }
)
_TERMINAL_TASK_STATES = frozenset({"completed", "failed", "killed", "stopped"})
_IGNORED_TASK_TYPES = frozenset({"monitor_mcp", "monitor_ws"})
_MINIMUM_QUESTIONS = 1
_MAXIMUM_QUESTIONS = 4
_MINIMUM_OPTIONS = 2
_MAXIMUM_OPTIONS = 4


def encode_claude_user_prompt(prompt: str) -> bytearray:
    """Encode one bounded official streaming-input user message."""
    if not prompt or "\0" in prompt:
        _malformed()
    frame = encode_compact_json_buffer(
        {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        }
    )
    frame.append(ord("\n"))
    if len(frame) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
        _malformed()
    return frame


def encode_claude_interrupt(request_id: RequestId) -> bytearray:
    """Encode one correlated in-process interrupt request."""
    return _encode_line(
        {
            "type": "control_request",
            "request_id": str(request_id),
            "request": {"subtype": "interrupt"},
        }
    )


def encode_claude_initialize(request_id: RequestId) -> bytearray:
    """Declare that the host supports no private dialog kinds."""
    return _encode_line(
        {
            "type": "control_request",
            "request_id": str(request_id),
            "request": {
                "subtype": "initialize",
                "supportedDialogKinds": [],
            },
        }
    )


def encode_claude_permission_response(
    request: ClaudeStructuredPermissionRequest,
    decision: ClaudeStructuredPermissionDecision,
) -> bytearray:
    """Encode one explicit correlated permission decision."""
    response: JsonObject
    if decision is ClaudeStructuredPermissionDecision.ALLOW:
        response = {
            "behavior": "allow",
            "updatedInput": decode_json_object(request.tool_input),
        }
    else:
        response = {
            "behavior": "deny",
            "message": "Denied by user.",
            "interrupt": False,
        }
    return _encode_line(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request.request_id,
                "response": response,
            },
        }
    )


def encode_claude_question_response(
    request: ClaudeStructuredQuestionRequest,
    answers: tuple[ClaudeStructuredQuestionAnswer, ...],
) -> bytearray:
    """Encode validated answers as an updated tool input."""
    expected = tuple(question.question for question in request.questions)
    if tuple(answer.question for answer in answers) != expected:
        _malformed()
    updated = dict(decode_json_object(request.permission.tool_input))
    updated["answers"] = {answer.question: answer.answer for answer in answers}
    annotations = {
        answer.question: {
            key: value
            for key, value in (
                ("preview", answer.preview),
                ("notes", answer.notes),
            )
            if value is not None
        }
        for answer in answers
        if answer.preview is not None or answer.notes is not None
    }
    if annotations:
        updated["annotations"] = annotations
    return _encode_control_response(
        request.permission.request_id,
        {
            "behavior": "allow",
            "updatedInput": updated,
        },
    )


def encode_claude_elicitation_decline(
    request: ClaudeStructuredElicitationRequest,
) -> bytearray:
    """Decline one unsupported elicitation without provider mutation."""
    return _encode_control_response(request.request_id, {"action": "decline"})


def encode_claude_dialog_unsupported(
    request: ClaudeStructuredDialogRequest,
) -> bytearray:
    """Return one typed error for an undeclared private dialog kind."""
    return _encode_line(
        {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request.request_id,
                "error": "Unsupported dialog kind.",
            },
        }
    )


def decode_claude_terminal_event(
    payload: bytes,
) -> ClaudeStructuredTerminalEvent:
    """Decode one exact-build stream frame for the local terminal host."""
    return ClaudeStructuredStreamDecoder().decode(payload)


class ClaudeStructuredStreamDecoder:
    """Decode exact stream frames while retaining task lifecycle kinds."""

    def __init__(self) -> None:
        self._tasks: dict[str, ClaudeStructuredActivityKind] = {}
        self._ignored_tasks: set[str] = set()
        self._stream_message_id: str | None = None
        self._streamed_blocks: set[tuple[str, int]] = set()

    def decode(self, payload: bytes) -> ClaudeStructuredTerminalEvent:
        """Decode one exact-build frame with sessionless lifecycle support."""
        return self._decode(payload)

    def _decode(self, payload: bytes) -> ClaudeStructuredTerminalEvent:
        """Decode one frame after retaining cross-frame task state."""
        if not payload or len(payload) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
            _malformed()
        try:
            root = decode_json_object(payload)
        except InvalidPayloadError:
            _malformed()
        message_type = _text(root, "type")
        if message_type not in _MESSAGE_TYPES:
            _malformed()
        conversation_id = _optional_conversation(root, message_type)
        (
            text,
            correlation,
            append,
            status,
            activities,
            control,
            cancelled_request_id,
            turn_complete,
        ) = self._event_content(root, message_type, conversation_id)
        return ClaudeStructuredTerminalEvent(
            conversation_id=conversation_id,
            text=text,
            text_correlation=correlation,
            text_append=append,
            status=status,
            activities=activities,
            control=control,
            cancelled_request_id=cancelled_request_id,
            turn_complete=turn_complete,
        )

    def _event_content(
        self,
        root: JsonObject,
        message_type: str,
        conversation_id: ClaudeStructuredConversationId | None,
    ) -> tuple[
        tuple[str, ...],
        str | None,
        bool,
        str | None,
        tuple[ClaudeStructuredStreamEvent, ...],
        ClaudeStructuredControlRequest | None,
        str | None,
        bool,
    ]:
        if message_type in {"control_request", "control_cancel_request"}:
            return (
                (),
                None,
                False,
                None,
                (),
                _control_request(root)
                if message_type == "control_request"
                else None,
                _bounded_text(root, "request_id")
                if message_type == "control_cancel_request"
                else None,
                False,
            )
        if (
            message_type in {"assistant", "user"}
            and conversation_id is not None
        ):
            if message_type == "assistant":
                text, activities = _assistant(
                    root,
                    conversation_id,
                    self._streamed_blocks,
                )
                return (
                    text,
                    None,
                    False,
                    None,
                    activities,
                    None,
                    None,
                    False,
                )
            return (
                (),
                None,
                False,
                None,
                _user(root, conversation_id),
                None,
                None,
                False,
            )
        if message_type == "system":
            status = _system_status(root)
            return (
                (),
                None,
                False,
                status,
                _system(
                    root,
                    conversation_id,
                    self._tasks,
                    self._ignored_tasks,
                ),
                None,
                None,
                False,
            )
        if message_type in {"stream_event", "result"}:
            if message_type == "stream_event":
                text, correlation, append, status = self._stream_content(root)
            else:
                text = _result_text(root)
                correlation = None
                append = False
                status = None
                self._stream_message_id = None
                self._streamed_blocks.clear()
            return (
                text,
                correlation,
                append,
                status,
                (),
                None,
                None,
                message_type == "result",
            )
        text, status = _presentation(root, message_type)
        return text, None, False, status, (), None, None, False

    def _stream_content(
        self,
        root: JsonObject,
    ) -> tuple[tuple[str, ...], str | None, bool, str | None]:
        event = _object(root, "event")
        event_type = _bounded_text(event, "type")
        if event_type == "message_start":
            self._stream_message_id = _bounded_text(
                _object(event, "message"),
                "id",
            )
            return (), None, False, None
        if event_type != "content_block_delta":
            return (), None, False, None
        index = _nonnegative_int(event, "index")
        delta = _object(event, "delta")
        delta_type = _bounded_text(delta, "type")
        if delta_type == "thinking_delta":
            _bounded_text(delta, "thinking")
            return (), None, False, "Claude is thinking."
        if delta_type != "text_delta":
            return (), None, False, None
        message_id = self._stream_message_id
        if message_id is None:
            _malformed()
        self._streamed_blocks.add((message_id, index))
        correlation = f"{message_id}:{index}"
        return (_text(delta, "text"),), correlation, True, None


def _control_request(root: JsonObject) -> ClaudeStructuredControlRequest:
    request = _object(root, "request")
    subtype = _bounded_text(request, "subtype")
    if subtype == "can_use_tool":
        return _permission(root, request)
    if subtype == "request_user_dialog":
        return _dialog(root, request)
    if subtype == "elicitation":
        return _elicitation(root, request)
    return _malformed()


def _permission(
    root: JsonObject,
    request: JsonObject,
) -> ClaudeStructuredPermissionRequest | ClaudeStructuredQuestionRequest:
    request_id = _bounded_text(root, "request_id")
    if not _PERMISSION_REQUIRED_KEYS.issubset(request) or not set(
        request
    ).issubset(_PERMISSION_REQUIRED_KEYS | _PERMISSION_OPTIONAL_KEYS):
        _malformed()
    tool_input = _object(request, "input")
    _validate_permission_options(request)
    suggestions = request.get("permission_suggestions", [])
    if not isinstance(suggestions, list):
        _malformed()
    permission = ClaudeStructuredPermissionRequest(
        request_id=request_id,
        tool_name=_bounded_text(request, "tool_name"),
        tool_use_id=_bounded_text(request, "tool_use_id"),
        tool_input=_freeze_json(tool_input),
        permission_suggestions=tuple(
            _freeze_json(value)
            for value in suggestions
            if isinstance(value, dict)
        ),
        agent_id=_optional_text(request, "agent_id"),
        blocked_path=_optional_text(request, "blocked_path"),
        decision_reason=_optional_text(request, "decision_reason"),
        description=_optional_text(request, "description"),
        display_name=_optional_text(request, "display_name"),
        title=_optional_text(request, "title"),
        requires_user_interaction=_optional_bool(
            request,
            "requires_user_interaction",
        ),
    )
    if permission.tool_name != "AskUserQuestion":
        return permission
    return _question_request(permission)


def _question_request(
    permission: ClaudeStructuredPermissionRequest,
) -> ClaudeStructuredQuestionRequest:
    tool_input = decode_json_object(permission.tool_input)
    if not set(tool_input).issubset(_QUESTION_INPUT_KEYS):
        _malformed()
    values = _list(tool_input, "questions")
    if not _MINIMUM_QUESTIONS <= len(values) <= _MAXIMUM_QUESTIONS:
        _malformed()
    questions = tuple(_question(value) for value in values)
    timeout = tool_input.get("afkTimeoutMs")
    if timeout is not None and (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        _malformed()
    return ClaudeStructuredQuestionRequest(
        permission=permission,
        questions=questions,
        afk_timeout_ms=timeout,
    )


def _question(value: JsonValue) -> ClaudeStructuredQuestion:
    root = _require_object(value)
    if set(root) != _QUESTION_KEYS:
        _malformed()
    option_values = _list(root, "options")
    if not _MINIMUM_OPTIONS <= len(option_values) <= _MAXIMUM_OPTIONS:
        _malformed()
    multi_select = root.get("multiSelect")
    if not isinstance(multi_select, bool):
        _malformed()
    return ClaudeStructuredQuestion(
        question=_bounded_text(root, "question"),
        header=_bounded_text(root, "header"),
        options=tuple(_question_option(option) for option in option_values),
        multi_select=multi_select,
    )


def _question_option(value: JsonValue) -> ClaudeStructuredQuestionOption:
    root = _require_object(value)
    if not {"label", "description"}.issubset(root) or not set(root).issubset(
        _QUESTION_OPTION_KEYS
    ):
        _malformed()
    return ClaudeStructuredQuestionOption(
        label=_bounded_text(root, "label"),
        description=_bounded_text(root, "description"),
        preview=_optional_text(root, "preview"),
    )


def _dialog(
    root: JsonObject,
    request: JsonObject,
) -> ClaudeStructuredDialogRequest:
    allowed = {"subtype", "dialog_kind", "payload", "tool_use_id"}
    if not {"subtype", "dialog_kind", "payload"}.issubset(request) or not set(
        request
    ).issubset(allowed):
        _malformed()
    return ClaudeStructuredDialogRequest(
        request_id=_bounded_text(root, "request_id"),
        dialog_kind=_bounded_text(request, "dialog_kind"),
        payload=_freeze_json(_object(request, "payload")),
        tool_use_id=_optional_text(request, "tool_use_id"),
    )


def _elicitation(
    root: JsonObject,
    request: JsonObject,
) -> ClaudeStructuredElicitationRequest:
    required = {
        "subtype",
        "mcp_server_name",
        "message",
        "mode",
        "url",
        "elicitation_id",
        "requested_schema",
        "title",
        "display_name",
        "description",
    }
    if set(request) != required:
        _malformed()
    return ClaudeStructuredElicitationRequest(
        request_id=_bounded_text(root, "request_id"),
        mcp_server_name=_bounded_text(request, "mcp_server_name"),
        message=_bounded_text(request, "message"),
        mode=_bounded_text(request, "mode"),
        url=_bounded_text(request, "url"),
        elicitation_id=_bounded_text(request, "elicitation_id"),
        requested_schema=_freeze_json(_object(request, "requested_schema")),
        title=_bounded_text(request, "title"),
        display_name=_bounded_text(request, "display_name"),
        description=_bounded_text(request, "description"),
    )


def _validate_permission_options(request: JsonObject) -> None:
    suggestions = request.get("permission_suggestions")
    if suggestions is not None and not (
        isinstance(suggestions, list)
        and all(isinstance(value, dict) for value in suggestions)
    ):
        _malformed()
    for name in _PERMISSION_OPTIONAL_KEYS - {
        "permission_suggestions",
        "requires_user_interaction",
    }:
        value = request.get(name)
        if value is not None and (
            not isinstance(value, str) or not value or "\0" in value
        ):
            _malformed()


def _assistant(
    root: JsonObject,
    conversation_id: ClaudeStructuredConversationId,
    streamed_blocks: set[tuple[str, int]],
) -> tuple[tuple[str, ...], tuple[ClaudeStructuredStreamEvent, ...]]:
    message = _object(root, "message")
    message_id = _bounded_text(message, "id")
    blocks = _list(message, "content")
    text: list[str] = []
    activities: list[ClaudeStructuredStreamEvent] = []
    for index, value in enumerate(blocks):
        block = _require_object(value)
        block_type = _text(block, "type")
        if block_type == "text" and (message_id, index) not in streamed_blocks:
            text.append(_text(block, "text"))
        elif block_type == "tool_use":
            activities.append(
                _activity(
                    conversation_id,
                    ClaudeStructuredActivityKind.TOOL,
                    _text(block, "id"),
                    ClaudeStructuredActivityState.STARTED,
                )
            )
    streamed_blocks.difference_update(
        (message_id, index) for index in range(len(blocks))
    )
    return tuple(text), tuple(activities)


def _user(
    root: JsonObject,
    conversation_id: ClaudeStructuredConversationId,
) -> tuple[ClaudeStructuredStreamEvent, ...]:
    message = _object(root, "message")
    content = message.get("content")
    if isinstance(content, str):
        return ()
    if not isinstance(content, list):
        _malformed()
    activities: list[ClaudeStructuredStreamEvent] = []
    for value in content:
        block = _require_object(value)
        if block.get("type") == "tool_result":
            activities.append(
                _activity(
                    conversation_id,
                    ClaudeStructuredActivityKind.TOOL,
                    _text(block, "tool_use_id"),
                    ClaudeStructuredActivityState.FINISHED,
                )
            )
    return tuple(activities)


def _system(
    root: JsonObject,
    conversation_id: ClaudeStructuredConversationId | None,
    tasks: dict[str, ClaudeStructuredActivityKind],
    ignored_tasks: set[str],
) -> tuple[ClaudeStructuredStreamEvent, ...]:
    subtype = _text(root, "subtype")
    if subtype.startswith("task_"):
        return _task_event(
            root,
            conversation_id,
            tasks,
            ignored_tasks,
            subtype,
        )
    if subtype == "hook_progress":
        _bounded_text(root, "hook_id")
        return ()
    if subtype in {"hook_started", "hook_response"}:
        return (
            _activity(
                conversation_id,
                ClaudeStructuredActivityKind.HOOK,
                _hook_id(root),
                (
                    ClaudeStructuredActivityState.STARTED
                    if subtype == "hook_started"
                    else ClaudeStructuredActivityState.FINISHED
                ),
            ),
        )
    return ()


def _task_event(
    root: JsonObject,
    conversation_id: ClaudeStructuredConversationId | None,
    tasks: dict[str, ClaudeStructuredActivityKind],
    ignored_tasks: set[str],
    subtype: str,
) -> tuple[ClaudeStructuredStreamEvent, ...]:
    events: tuple[ClaudeStructuredStreamEvent, ...] = ()
    if subtype == "task_started":
        task_id = _bounded_text(root, "task_id")
        task_type = _optional_text(root, "task_type")
        if task_type in _IGNORED_TASK_TYPES:
            ignored_tasks.add(task_id)
        elif task_id not in tasks:
            kind = _task_kind(task_type)
            tasks[task_id] = kind
            events = (
                _activity(
                    conversation_id,
                    kind,
                    task_id,
                    ClaudeStructuredActivityState.STARTED,
                ),
            )
    elif subtype in {"task_notification", "task_updated"}:
        task_id = _bounded_text(root, "task_id")
        if task_id in ignored_tasks:
            if subtype == "task_notification" or _task_update_terminal(root):
                ignored_tasks.discard(task_id)
        else:
            kind = tasks.get(task_id)
            terminal = subtype == "task_notification"
            if subtype == "task_updated":
                terminal = _task_update_terminal(root)
            if kind is not None and terminal:
                del tasks[task_id]
                events = (
                    _activity(
                        conversation_id,
                        kind,
                        task_id,
                        ClaudeStructuredActivityState.FINISHED,
                    ),
                )
    return events


def _task_update_terminal(root: JsonObject) -> bool:
    return _object(root, "patch").get("status") in _TERMINAL_TASK_STATES


def _stream_text(root: JsonObject) -> tuple[str, ...]:
    event = _object(root, "event")
    if event.get("type") != "content_block_delta":
        return ()
    delta = _object(event, "delta")
    if delta.get("type") != "text_delta":
        return ()
    return (_text(delta, "text"),)


def _result_text(root: JsonObject) -> tuple[str, ...]:
    is_error = root.get("is_error")
    if not isinstance(is_error, bool):
        _malformed()
    if not is_error:
        return ()
    return ("Claude could not complete the request.",)


def _system_status(root: JsonObject) -> str | None:
    subtype = _bounded_text(root, "subtype")
    if subtype == "status":
        return _status_presentation(root)
    if subtype == "thinking_tokens":
        _nonnegative_int(root, "estimated_tokens")
        _nonnegative_int(root, "estimated_tokens_delta")
        return "Claude is thinking."
    if subtype == "notification":
        return _bounded_text(root, "text")
    return {
        "hook_progress": "A Claude hook is running.",
        "task_progress": "A Claude background task is running.",
        "compact_boundary": "Claude compacted the conversation.",
    }.get(subtype)


def _status_presentation(root: JsonObject) -> str | None:
    status = root.get("status")
    if status == "compacting":
        return "Claude is compacting the conversation."
    if status == "requesting":
        return "Claude is requesting a response."
    mode = root.get("permissionMode")
    if mode == "plan":
        return "Claude entered plan mode."
    if isinstance(mode, str):
        return "Claude left plan mode."
    return None


def _presentation(
    root: JsonObject,
    message_type: str,
) -> tuple[tuple[str, ...], str | None]:
    if message_type == "prompt_suggestion":
        return (
            (f"Suggestion: {_bounded_text(root, 'suggestion')}",),
            None,
        )
    if message_type == "auth_status":
        return (), _auth_status(root)
    if message_type == "rate_limit_event":
        return (), _rate_limit_status(root)
    if message_type == "tool_progress":
        tool_name = _bounded_text(root, "tool_name")
        elapsed = _nonnegative_int(root, "elapsed_time_seconds")
        return (), f"{tool_name} has been running for {elapsed}s."
    if message_type == "tool_use_summary":
        _bounded_text(root, "summary")
        return (), "Claude completed a tool operation."
    return (), None


def _auth_status(root: JsonObject) -> str:
    authenticating = root.get("isAuthenticating")
    output = root.get("output")
    if not isinstance(authenticating, bool) or not (
        isinstance(output, list)
        and all(isinstance(value, str) for value in output)
    ):
        _malformed()
    if root.get("error") is not None:
        _bounded_text(root, "error")
        return "Claude authentication requires attention."
    if authenticating:
        return "Claude authentication is in progress."
    return "Claude authentication status changed."


def _rate_limit_status(root: JsonObject) -> str:
    status = _bounded_text(_object(root, "rate_limit_info"), "status")
    if status == "rejected":
        return "Claude usage limits currently block requests."
    if status == "allowed_warning":
        return "Claude usage limits are nearly exhausted."
    if status != "allowed":
        _malformed()
    return "Claude usage limits allow requests."


def _conversation(root: JsonObject) -> ClaudeStructuredConversationId:
    try:
        return ClaudeStructuredConversationId(_text(root, "session_id"))
    except ValueError:
        _malformed()


def _activity(
    conversation_id: ClaudeStructuredConversationId | None,
    kind: ClaudeStructuredActivityKind,
    activity_id: str,
    state: ClaudeStructuredActivityState,
) -> ClaudeStructuredStreamEvent:
    return ClaudeStructuredStreamEvent(
        conversation_id=conversation_id,
        activity_kind=kind,
        activity_id=activity_id,
        activity_state=state,
    )


def _hook_id(root: JsonObject) -> str:
    return _bounded_text(root, "hook_id")


def _task_kind(task_type: str | None) -> ClaudeStructuredActivityKind:
    if task_type == "local_agent":
        return ClaudeStructuredActivityKind.BACKGROUND_AGENT
    if task_type == "local_bash":
        return ClaudeStructuredActivityKind.TERMINAL
    return ClaudeStructuredActivityKind.BACKGROUND_TASK


def _optional_conversation(
    root: JsonObject,
    message_type: str,
) -> ClaudeStructuredConversationId | None:
    value = root.get("session_id")
    if value is None and message_type in {
        "control_cancel_request",
        "control_request",
        "system",
    }:
        return None
    return _conversation(root)


def _optional_text(root: JsonObject, name: str) -> str | None:
    return None if root.get(name) is None else _bounded_text(root, name)


def _optional_bool(root: JsonObject, name: str) -> bool | None:
    value = root.get(name)
    if value is not None and not isinstance(value, bool):
        _malformed()
    return value


def _text(root: JsonObject, name: str) -> str:
    value = root.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        _malformed()
    return value


def _bounded_text(root: JsonObject, name: str) -> str:
    value = _text(root, name)
    try:
        require_bounded_text(
            value,
            name=f"Claude structured {name}",
            maximum=MAX_OPAQUE_BYTES,
        )
    except TypeError, ValueError:
        _malformed()
    return value


def _encode_line(root: JsonObject) -> bytearray:
    frame = encode_compact_json_buffer(root)
    frame.append(ord("\n"))
    if len(frame) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
        clear_secret_buffer(frame)
        _malformed()
    return frame


def _encode_control_response(
    request_id: str,
    response: JsonObject,
) -> bytearray:
    return _encode_line(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
    )


def _freeze_json(root: JsonObject) -> bytes:
    return bytes(encode_compact_json_buffer(root))


def _object(root: JsonObject, name: str) -> JsonObject:
    return _require_object(root.get(name))


def _require_object(value: JsonValue | None) -> JsonObject:
    if not isinstance(value, dict):
        _malformed()
    return value


def _list(root: JsonObject, name: str) -> list[JsonValue]:
    value = root.get(name)
    if not isinstance(value, list):
        _malformed()
    return value


def _nonnegative_int(root: JsonObject, name: str) -> int:
    value = root.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _malformed()
    return value


def _malformed() -> NoReturn:
    raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_MALFORMED)
