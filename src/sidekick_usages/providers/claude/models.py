"""Immutable Claude CLI and setup-token models."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.platform.models import ExecutableProvenance


@dataclass(frozen=True, slots=True, order=True)
class ClaudeVersion:
    """One exact semantic Claude CLI version."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        """Reject invalid semantic-version components."""
        if min(self.major, self.minor, self.patch) < 0:
            raise ValueError("Claude version components cannot be negative.")

    def __str__(self) -> str:
        """Render the canonical numeric semantic version."""
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class ClaudeExecutable:
    """One exact Claude executable and release version."""

    launcher: Path
    provenance: ExecutableProvenance
    version: ClaudeVersion

    def __post_init__(self) -> None:
        """Require the stable launcher path preserved at discovery."""
        if not self.launcher.is_absolute():
            raise ValueError("Claude launcher path must be absolute.")


@dataclass(frozen=True, slots=True)
class ClaudeNativeProfile:
    """One explicit native Claude configuration profile."""

    config_directory: Path

    def __post_init__(self) -> None:
        """Require one explicit absolute configuration directory."""
        if not self.config_directory.is_absolute():
            raise ValueError("Claude native profile path must be absolute.")


@dataclass(frozen=True, slots=True)
class ClaudeManagedProfile:
    """One stable Sidekick-owned Claude configuration profile."""

    account_id: SidekickAccountId
    config_directory: Path

    def __post_init__(self) -> None:
        """Require one absolute account-ID-derived directory."""
        if (
            not self.config_directory.is_absolute()
            or self.config_directory.name != str(self.account_id)
        ):
            raise ValueError("Claude managed profile path is invalid.")


@dataclass(frozen=True, slots=True)
class ClaudeCommandResult:
    """One bounded Claude process result."""

    return_code: int
    output: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SetupTokenSuccess:
    """A Claude setup-token process yielded one validated token."""

    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SetupTokenMissing:
    """Claude setup-token completed without a recognizable token."""


@dataclass(frozen=True, slots=True)
class SetupTokenRejected:
    """Claude setup-token exited unsuccessfully."""

    return_code: int


@dataclass(frozen=True, slots=True)
class SetupTokenTimedOut:
    """Claude setup-token exceeded its bounded execution time."""


@dataclass(frozen=True, slots=True)
class SetupTokenUnreadable:
    """Claude setup-token could not produce bounded safe output."""
