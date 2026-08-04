"""Strict secret-free models for structured Claude control."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.identifiers import CanonicalUuid
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.models import ClaudeExecutable

type ClaudeStructuredControlRequest = (
    ClaudeStructuredPermissionRequest
    | ClaudeStructuredQuestionRequest
    | ClaudeStructuredDialogRequest
    | ClaudeStructuredElicitationRequest
    | ClaudeStructuredHookCallbackRequest
    | ClaudeStructuredMcpMessageRequest
)


class ClaudeStructuredFailure(StrEnum):
    """Closed redacted failures at the structured provider boundary."""

    ACTIVITY_ACTIVE = "activity_active"
    ACTIVITY_INVALID = "activity_invalid"
    AUTHORITY_MISMATCH = "authority_mismatch"
    CONVERSATION_MISMATCH = "conversation_mismatch"
    PROTOCOL_MALFORMED = "protocol_malformed"
    PROTOCOL_TIMEOUT = "protocol_timeout"
    PROTOCOL_EOF = "protocol_eof"
    PROCESS_EXITED = "process_exited"
    PROCESS_UNAVAILABLE = "process_unavailable"
    VERSION_UNSUPPORTED = "version_unsupported"


class ClaudeStructuredError(RuntimeError):
    """One typed structured failure containing no provider data."""

    def __init__(self, code: ClaudeStructuredFailure) -> None:
        self.code = code
        super().__init__(_failure_message(code))


class ClaudeStructuredActivityKind(StrEnum):
    """Provider activity categories that prevent an OAuth update."""

    BACKGROUND_AGENT = "background_agent"
    BACKGROUND_TASK = "background_task"
    PERMISSION = "permission"
    DIALOG = "dialog"
    HOOK = "hook"
    TOOL = "tool"
    MCP = "mcp"
    TERMINAL = "terminal"


class ClaudeStructuredActivityState(StrEnum):
    """Closed activity transitions derived from provider stream events."""

    STARTED = "started"
    FINISHED = "finished"


class ClaudeStructuredConversationId(CanonicalUuid):
    """Stable conversation identity emitted by one structured engine."""

    _name = "Claude structured conversation ID"


class ClaudeStructuredPermissionDecision(StrEnum):
    """One explicit local answer to a provider permission request."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredQuestionOption:
    """One exact option exposed by ``AskUserQuestion``."""

    label: str
    description: str
    preview: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredQuestion:
    """One validated question exposed through the permission channel."""

    question: str
    header: str
    options: tuple[ClaudeStructuredQuestionOption, ...]
    multi_select: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredPermissionRequest:
    """One correlated provider request requiring terminal consent."""

    request_id: str
    tool_name: str
    tool_use_id: str
    tool_input: bytes = field(repr=False)
    permission_suggestions: tuple[bytes, ...] = field(
        default=(),
        repr=False,
    )
    agent_id: str | None = None
    blocked_path: str | None = None
    decision_reason: str | None = None
    description: str | None = None
    display_name: str | None = None
    title: str | None = None
    requires_user_interaction: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredQuestionRequest:
    """One correlated and validated ``AskUserQuestion`` request."""

    permission: ClaudeStructuredPermissionRequest
    questions: tuple[ClaudeStructuredQuestion, ...]
    afk_timeout_ms: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredDialogRequest:
    """One unexpected private dialog envelope declared unsupported."""

    request_id: str
    dialog_kind: str
    payload: bytes = field(repr=False)
    tool_use_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredHookCallbackRequest:
    """One undeclared provider hook callback refused by the host."""

    request_id: str
    callback_id: str
    tool_use_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredMcpMessageRequest:
    """One message for an SDK MCP server the host did not declare."""

    request_id: str
    server_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredElicitationRequest:
    """One MCP elicitation request that Sidekick safely declines."""

    request_id: str
    mcp_server_name: str
    message: str
    mode: str
    url: str
    elicitation_id: str
    requested_schema: bytes = field(repr=False)
    title: str
    display_name: str
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredQuestionAnswer:
    """One validated answer returned to ``AskUserQuestion``."""

    question: str
    answer: str
    preview: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredStreamEvent:
    """One strict provider activity transition for a conversation."""

    conversation_id: ClaudeStructuredConversationId | None
    activity_kind: ClaudeStructuredActivityKind
    activity_id: str
    activity_state: ClaudeStructuredActivityState


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredTerminalEvent:
    """One provider-decoded event safe for terminal presentation."""

    conversation_id: ClaudeStructuredConversationId | None
    text: tuple[str, ...]
    text_correlation: str | None = None
    text_append: bool = False
    status: str | None = None
    activities: tuple[ClaudeStructuredStreamEvent, ...] = ()
    control: ClaudeStructuredControlRequest | None = None
    cancelled_request_id: str | None = None
    turn_complete: bool = False
    authoritative_idle: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredBinding:
    """Post-commit authority bound to one participant epoch."""

    operation_id: OperationId
    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredInstallReceipt:
    """Correlated local OAuth installation for one exact binding."""

    binding: ClaudeStructuredBinding
    request_id: RequestId


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredAdoptionReceipt:
    """First-real-turn routing evidence produced before transmission."""

    turn_id: TurnId
    binding: ClaudeStructuredBinding


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredCapability:
    """Exact-build evidence enabling private structured OAuth control."""

    executable: ClaudeExecutable
    host: HostPlatform
    artifact_sha256: str
    embedded_build_time: str
    embedded_git_sha: str
    variable_allowlist: tuple[str, ...]


class ClaudeStructuredEngine(Protocol):
    """Pipe-owned official engine operations used by one session."""

    @property
    def process_id(self) -> int:
        """Return the unchanged official engine PID."""

    def exchange(
        self,
        request: bytearray,
        request_id: RequestId,
        timeout_seconds: float,
    ) -> bytes:
        """Exchange one correlated bounded control frame without retry."""

    def send_interactive(
        self,
        frame: bytearray,
        timeout_seconds: float,
    ) -> None:
        """Send one bounded typed interactive frame."""

    def receive_event(self, timeout_seconds: float) -> bytes:
        """Return one bounded typed interactive event frame."""

    def close_input(self) -> None:
        """Close the structured input without signalling the child."""

    def wait(self, timeout_seconds: float) -> int:
        """Return the official child process's ordinary exit status."""

    def dispose_unenrolled(self) -> None:
        """Dispose one child whose ownership never reached a live session."""


class ClaudeStructuredProtectedFrame(Protocol):
    """Single-use mutable OAuth projection received on a protected channel."""

    @property
    def protected_binding(self) -> ClaudeStructuredBinding:
        """Return the secret-free authority binding on this frame."""

    def take_protected_oauth(self) -> bytearray:
        """Transfer the protected mutable OAuth buffer exactly once."""

    def close_protected_frame(self) -> None:
        """Clear every credential reference retained by the frame."""


class ClaudeStructuredEngineFactory(Protocol):
    """Launch one separate exact-build structured child."""

    def __call__(
        self,
        executable: ClaudeExecutable,
        environment: Mapping[str, str],
        *,
        working_directory: Path,
        user_arguments: tuple[str, ...] = (),
    ) -> ClaudeStructuredEngine:
        """Return one pipe-owned official structured engine."""


class ClaudeStructuredTurnTransmitter(Protocol):
    """Host callback receiving adoption before provider transmission."""

    def __call__(
        self,
        receipt: ClaudeStructuredAdoptionReceipt,
    ) -> None:
        """Submit adoption, then transmit the real admitted prompt."""


def _failure_message(code: ClaudeStructuredFailure) -> str:
    return {
        ClaudeStructuredFailure.ACTIVITY_ACTIVE: (
            "The structured Claude participant is not idle."
        ),
        ClaudeStructuredFailure.ACTIVITY_INVALID: (
            "The structured Claude activity transition is invalid."
        ),
        ClaudeStructuredFailure.AUTHORITY_MISMATCH: (
            "The structured Claude authority binding does not match."
        ),
        ClaudeStructuredFailure.CONVERSATION_MISMATCH: (
            "The structured Claude conversation identity does not match."
        ),
        ClaudeStructuredFailure.PROTOCOL_MALFORMED: (
            "The structured Claude control response is invalid."
        ),
        ClaudeStructuredFailure.PROTOCOL_TIMEOUT: (
            "The structured Claude control response timed out."
        ),
        ClaudeStructuredFailure.PROTOCOL_EOF: (
            "The structured Claude control stream closed unexpectedly."
        ),
        ClaudeStructuredFailure.PROCESS_EXITED: (
            "The structured Claude process exited."
        ),
        ClaudeStructuredFailure.PROCESS_UNAVAILABLE: (
            "The structured Claude process is unavailable."
        ),
        ClaudeStructuredFailure.VERSION_UNSUPPORTED: (
            "The installed Claude build is not qualified for control."
        ),
    }[code]
