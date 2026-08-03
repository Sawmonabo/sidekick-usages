"""Strict secret-free models for structured Claude control."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.models import ClaudeExecutable


class ClaudeStructuredFailure(StrEnum):
    """Closed redacted failures at the structured provider boundary."""

    ACTIVITY_ACTIVE = "activity_active"
    ACTIVITY_INVALID = "activity_invalid"
    AUTHORITY_MISMATCH = "authority_mismatch"
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredBinding:
    """Post-commit authority bound to one participant epoch."""

    operation_id: OperationId
    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeStructuredReadyReceipt:
    """Correlated cache-clear acknowledgement for one exact binding."""

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

    def close_input(self) -> None:
        """Close the structured input without signalling the child."""

    def wait(self, timeout_seconds: float) -> int:
        """Return the official child process's ordinary exit status."""


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
