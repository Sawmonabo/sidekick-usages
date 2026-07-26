"""Operating-system detection for per-user service integration."""

import getpass
import os
import platform
import shutil
import stat
from pathlib import Path

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
from sidekick_usages.platform.host import is_wsl

_SYSTEMD_RUNTIME = Path("/run/systemd/system")


class FeatureDisabledBackend:
    """Explicit disabled integration for unsupported native platforms."""

    id = ServiceBackendId.FEATURE_DISABLED

    def cancel(self) -> None:
        """Leave unsupported native service state unchanged."""

    def install(self) -> None:
        """Leave unsupported native service state unchanged."""

    def restart(self) -> None:
        """Leave unsupported native service state unchanged."""

    def status(self) -> ServiceBackendStatus:
        """Return the explicit feature-disabled state."""
        return ServiceBackendStatus.single(
            self.id,
            ServiceLifecycleState.FEATURE_DISABLED,
        )

    def uninstall(self) -> None:
        """Leave unsupported native service state unchanged."""


def detect_platform_info() -> PlatformInfo:
    """Resolve exact current-user facts without changing system state."""
    system = platform.system()
    uid = _effective_user_id()
    is_wsl_host = system == "Linux" and is_wsl(
        platform.release(),
        os.environ,
    )
    return PlatformInfo(
        system=system,
        home=Path.home().resolve(strict=True),
        uid=uid,
        user_name=_user_name(system, uid),
        is_wsl=is_wsl_host,
        wsl_distro=(
            os.environ.get("WSL_DISTRO_NAME") if is_wsl_host else None
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
        raise ServiceLifecycleError(ServiceFailureCode.EXECUTABLE_UNAVAILABLE)
    return qualify_supervisor_executable(Path(candidate))


def qualify_supervisor_executable(candidate: Path) -> Path:
    """Resolve and validate one exact supervisor console-script path."""
    if not candidate.is_absolute() or candidate.name != SUPERVISOR_ENTRY_POINT:
        raise ServiceLifecycleError(ServiceFailureCode.EXECUTABLE_UNAVAILABLE)
    try:
        executable = candidate.resolve(strict=True)
        metadata = executable.stat()
    except OSError:
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(
        executable, os.X_OK
    ):
        raise ServiceLifecycleError(ServiceFailureCode.EXECUTABLE_UNAVAILABLE)
    return executable


def _effective_user_id() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else 0


def _user_name(system: str, uid: int) -> str:
    if system == "Linux":
        process_path = Path("/proc/self")
        try:
            if process_path.stat().st_uid != uid:
                raise ServiceLifecycleError(ServiceFailureCode.ARTIFACT_UNSAFE)
            return process_path.owner()
        except OSError, KeyError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None
    return getpass.getuser()
