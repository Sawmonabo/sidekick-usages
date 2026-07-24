"""Operating-system detection for per-user service integration."""

import getpass
import os
import platform
import shutil
import stat
import sys
from pathlib import Path

if sys.platform != "win32":
    import pwd

from sidekick_usages.daemon.lifecycle.constants import (
    SUPERVISOR_ENTRY_POINT,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import (
    PlatformInfo,
    ServiceBackendStatus,
)
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecycleState,
)

__all__ = [
    "FeatureDisabledBackend",
    "detect_platform_info",
    "qualify_supervisor_executable",
    "resolve_supervisor_executable",
]

_SYSTEMD_RUNTIME = Path("/run/systemd/system")
_WSL_OS_RELEASE = Path("/proc/sys/kernel/osrelease")


class FeatureDisabledBackend:
    """Explicit disabled integration for unsupported native platforms."""

    id = ServiceBackendId.FEATURE_DISABLED

    def install(self) -> None:
        """Leave unsupported native service state unchanged."""

    def restart(self) -> None:
        """Leave unsupported native service state unchanged."""

    def status(self) -> ServiceBackendStatus:
        """Return the explicit feature-disabled state."""
        return ServiceBackendStatus(
            self.id,
            ServiceLifecycleState.FEATURE_DISABLED,
        )

    def uninstall(self) -> None:
        """Leave unsupported native service state unchanged."""


def detect_platform_info() -> PlatformInfo:
    """Resolve exact current-user facts without changing system state."""
    system = platform.system()
    uid = _effective_user_id()
    is_wsl = system == "Linux" and _detect_wsl()
    return PlatformInfo(
        system=system,
        home=Path.home().resolve(strict=True),
        uid=uid,
        user_name=_user_name(uid),
        is_wsl=is_wsl,
        wsl_distro=(
            os.environ.get("WSL_DISTRO_NAME") if is_wsl else None
        ),
        has_user_systemd=(
            system == "Linux"
            and shutil.which("systemctl") is not None
            and _SYSTEMD_RUNTIME.is_dir()
        ),
    )


def resolve_supervisor_executable() -> Path:
    """Resolve the exact installed supervisor console script."""
    candidate = shutil.which(SUPERVISOR_ENTRY_POINT)
    if candidate is None:
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        )
    return qualify_supervisor_executable(Path(candidate))


def qualify_supervisor_executable(candidate: Path) -> Path:
    """Resolve and validate one exact supervisor console-script path."""
    if (
        not candidate.is_absolute()
        or candidate.name != SUPERVISOR_ENTRY_POINT
    ):
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        )
    try:
        executable = candidate.resolve(strict=True)
        metadata = executable.stat()
    except OSError:
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        ) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(executable, os.X_OK)
    ):
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        )
    return executable


def _effective_user_id() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else 0


def _user_name(uid: int) -> str:
    if sys.platform != "win32":
        return pwd.getpwuid(uid).pw_name
    return getpass.getuser()


def _detect_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = _WSL_OS_RELEASE.read_text(
            encoding="utf-8",
            errors="strict",
        )
    except OSError, UnicodeError:
        return False
    return "microsoft" in release.casefold()
