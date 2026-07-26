"""Secret-safe Claude activation runtime and failures."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_FAILURE_CODE_PREFIX = "claude_activation_"


class ClaudeActivationFailure(StrEnum):
    """Closed safe failures from native Claude activation."""

    INCOMPATIBLE = "incompatible"
    NATIVE_CHANGED = "native_changed"
    NATIVE_UNAVAILABLE = "native_unavailable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STATE_CHANGED = "state_changed"
    TARGET_UNAVAILABLE = "target_unavailable"
    TIMED_OUT = "timed_out"

    @property
    def action_required(self) -> bool:
        """Return whether activation requires user repair."""
        return self in {
            ClaudeActivationFailure.INCOMPATIBLE,
            ClaudeActivationFailure.NATIVE_CHANGED,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        }

    @property
    def failure_code(self) -> str:
        """Return the complete sanitized worker and journal code."""
        return _FAILURE_CODE_PREFIX + self.value


class ClaudeActivationError(RuntimeError):
    """One secret-safe native Claude activation failure."""

    def __init__(self, failure: ClaudeActivationFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)

    @property
    def action_required(self) -> bool:
        """Return whether the user must repair this activation."""
        return self.failure.action_required

    @property
    def timed_out(self) -> bool:
        """Return whether the official provider operation timed out."""
        return self.failure is ClaudeActivationFailure.TIMED_OUT

    @property
    def failure_code(self) -> str:
        """Return the safe worker and journal outcome code."""
        return self.failure.failure_code


@dataclass(frozen=True, slots=True)
class ClaudeActivationRuntime:
    """Injectable host and provider boundaries for one activation worker."""

    environment: Mapping[str, str] | None = None
    host: HostPlatform | None = None
    runner: ClaudeCommandRunner = run_bounded_claude_command
