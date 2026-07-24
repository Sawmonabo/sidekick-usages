"""Secret-free resident-service lifecycle models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceLifecycleState,
)

__all__ = [
    "CommandResult",
    "DaemonOperationResult",
    "PlatformInfo",
    "ServiceArtifact",
    "ServiceBackendStatus",
]

_MAX_ARTIFACT_BYTES = 256 * 1024
_MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
_MAX_IDENTITY_BYTES = 256
_MAX_MESSAGE_BYTES = 1024
_SERVICE_ARTIFACT_MODE = 0o600


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed bounded native command result."""

    returncode: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        """Require bounded native output and an integer process result."""
        if type(self.returncode) is not int:
            raise ValueError("Command return code must be an integer.")
        _require_text_bound(
            self.stdout,
            "Command standard output",
            _MAX_COMMAND_OUTPUT_BYTES,
            allow_empty=True,
        )
        _require_text_bound(
            self.stderr,
            "Command standard error",
            _MAX_COMMAND_OUTPUT_BYTES,
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """Platform facts used to select one user-service integration."""

    system: str
    home: Path
    uid: int
    user_name: str
    is_wsl: bool
    wsl_distro: str | None
    has_user_systemd: bool

    def __post_init__(self) -> None:
        """Validate exact local identities without guessing WSL values."""
        if not self.home.is_absolute():
            raise ValueError("Platform home must be absolute.")
        if self.uid < 0:
            raise ValueError("Platform user ID cannot be negative.")
        _require_identity(self.system, "Platform system")
        _require_identity(self.user_name, "Platform user name")
        if self.is_wsl:
            if self.system != "Linux" or self.wsl_distro is None:
                raise ValueError(
                    "WSL requires Linux and an explicit distribution."
                )
            _require_identity(self.wsl_distro, "WSL distribution")
        elif self.wsl_distro is not None:
            raise ValueError("Non-WSL platforms cannot name a distribution.")


@dataclass(frozen=True, slots=True)
class ServiceArtifact:
    """One exact generated user-service definition."""

    path: Path
    payload: bytes
    mode: int = _SERVICE_ARTIFACT_MODE

    def __post_init__(self) -> None:
        """Require one bounded absolute owner-only artifact."""
        if not self.path.is_absolute():
            raise ValueError("Service artifact path must be absolute.")
        if not self.payload or len(self.payload) > _MAX_ARTIFACT_BYTES:
            raise ValueError("Service artifact payload is outside its bound.")
        if self.mode != _SERVICE_ARTIFACT_MODE:
            raise ValueError("Service artifacts must be owner-only.")


@dataclass(frozen=True, slots=True)
class ServiceBackendStatus:
    """Read-only state for one complete platform integration."""

    backend: ServiceBackendId
    state: ServiceLifecycleState

    def __post_init__(self) -> None:
        """Require closed backend and lifecycle values."""
        if not isinstance(self.backend, ServiceBackendId) or not isinstance(
            self.state,
            ServiceLifecycleState,
        ):
            raise ValueError("Service status values are invalid.")


@dataclass(frozen=True, slots=True)
class DaemonOperationResult:
    """Safe result returned by a public daemon lifecycle command."""

    backend: ServiceBackendId
    state: ServiceLifecycleState
    message: str
    exit_code: ExitCode = ExitCode.SUCCESS

    def __post_init__(self) -> None:
        """Require safe bounded presentation state."""
        if not isinstance(self.backend, ServiceBackendId) or not isinstance(
            self.state,
            ServiceLifecycleState,
        ):
            raise ValueError("Daemon result state is invalid.")
        if not isinstance(self.exit_code, ExitCode):
            raise ValueError("Daemon exit code is invalid.")
        _require_text_bound(
            self.message,
            "Daemon result message",
            _MAX_MESSAGE_BYTES,
        )


def _require_identity(value: str, name: str) -> None:
    _require_text_bound(value, name, _MAX_IDENTITY_BYTES)
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} is invalid.")


def _require_text_bound(
    value: str,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8.") from None
    if (not encoded and not allow_empty) or len(encoded) > maximum:
        raise ValueError(f"{name} is outside its size bound.")
