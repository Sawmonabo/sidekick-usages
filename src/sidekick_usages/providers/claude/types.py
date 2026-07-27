"""Closed Claude CLI and setup-token types."""

from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeExecutable,
    ClaudeManagedProfile,
    ClaudeNativeProfile,
    SetupTokenMissing,
    SetupTokenRejected,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)

type ClaudeProfile = ClaudeNativeProfile | ClaudeManagedProfile
type SetupTokenCapture = (
    SetupTokenSuccess
    | SetupTokenMissing
    | SetupTokenRejected
    | SetupTokenTimedOut
    | SetupTokenUnreadable
)


class ClaudeProcessFailure(StrEnum):
    """Safe reasons a bounded Claude command failed."""

    CANCELLED = "cancelled"
    PROCESS_UNAVAILABLE = "process_unavailable"
    PROCESS_UNSAFE = "process_unsafe"
    OUTPUT_TOO_LARGE = "output_too_large"
    OUTPUT_UNREADABLE = "output_unreadable"
    TIMED_OUT = "timed_out"


class ClaudeCommandRunner(Protocol):
    """Run one bounded Claude command without exposing raw output."""

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
        cancelled: Callable[[], bool] | None = None,
    ) -> ClaudeCommandResult:
        """Return one bounded process result or raise a typed failure."""


class ClaudeExecutableDiscovery(Protocol):
    """Resolve one exact Claude executable for a capability proof."""

    def __call__(
        self,
        environment: Mapping[str, str] | None,
        *,
        working_directory: Path | None,
        runner: ClaudeCommandRunner,
        cancelled: Callable[[], bool] | None,
    ) -> ClaudeExecutable:
        """Return the qualified executable or raise a typed failure."""


class ClaudeInteractiveCommandRunner(Protocol):
    """Run one bounded Claude command with inherited terminal streams."""

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
    ) -> int:
        """Return the provider process exit status."""


class ClaudeSetupToken(Protocol):
    """Narrow structural capability for Claude setup-token capture."""

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        """Capture one typed Claude setup-token outcome."""
