"""Typed session launch and shell enrollment values."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.platform.models import ExecutableProvenance


class SessionLaunchFailure(StrEnum):
    """Closed prelaunch refusal reasons."""

    INVALID_ARGUMENT = "invalid_argument"
    RECURSIVE_EXECUTABLE = "recursive_executable"
    UNSAFE_OVERRIDE = "unsafe_override"
    EXECUTABLE_CHANGED = "executable_changed"


class SessionLaunchError(UsageError):
    """Refuse one unsafe provider launch before process execution."""

    def __init__(
        self,
        code: SessionLaunchFailure,
        detail: str,
    ) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class SessionLaunchSpec:
    """Frozen executable, argv, environment, and working-directory plan."""

    provider_id: ProviderId
    launcher: Path
    executable: ExecutableProvenance
    provider_arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: Path

    @property
    def command(self) -> tuple[str, ...]:
        """Return the exact provider command without shell interpretation."""
        return (str(self.executable.path), *self.provider_arguments)


class ShellKind(StrEnum):
    """Supported initial interactive shell integrations."""

    BASH = "bash"
    ZSH = "zsh"
    FISH = "fish"


class ShellIntegrationFailure(StrEnum):
    """Closed shell resolution and mutation refusal reasons."""

    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    UNSAFE_PATH = "unsafe_path"
    SOURCE_CHANGED = "source_changed"
    FILESYSTEM = "filesystem"


class ShellIntegrationError(UsageError):
    """Refuse an unsafe or ambiguous shell integration change."""

    def __init__(
        self,
        code: ShellIntegrationFailure,
        message: str,
        *,
        path: Path | None = None,
        manual_range: tuple[int, int] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.manual_range = manual_range
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedShellStartup:
    """Exact startup and generated-file targets for one shell."""

    kind: ShellKind
    startup_file: Path
    generated_file: Path
    startup_root: Path
    generated_root: Path
    requires_source_block: bool


@dataclass(frozen=True, slots=True)
class ShellIntegrationResult:
    """One applied or previewed shell enrollment operation."""

    changed: bool
    dry_run: bool
    paths: tuple[Path, ...]
    preconditions: tuple[str, ...]
    diffs: tuple[str, ...]


class ShellEnrollmentState(StrEnum):
    """Read-only shell enrollment states."""

    INTEGRATED = "integrated"
    NOT_LOADED = "not_loaded"
    BYPASSED = "bypassed"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ShellEnrollmentStatus:
    """Secret-free enrollment status for one shell selection."""

    state: ShellEnrollmentState
    paths: tuple[Path, ...]
    detail: str
