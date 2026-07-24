"""Scheduled-maintenance boundary models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.types import ExitCode

__all__ = [
    "CommandResult",
    "DaemonOperationResult",
    "PlatformInfo",
]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed system command result."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class DaemonOperationResult:
    """Result of a scheduled-maintenance manager operation."""

    backend: str
    message: str
    exit_code: ExitCode = ExitCode.SUCCESS


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """Platform facts used by scheduler backend selection."""

    system: str
    home: Path
    uid: int
    is_wsl: bool
    wsl_distro: str | None
    has_user_systemd: bool
