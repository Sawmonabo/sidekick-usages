"""Single host-platform classifier for local integrations."""

import os
import platform
from collections.abc import Mapping

from sidekick_usages.platform.types import HostPlatform

_MACOS_ARM64_MACHINES = frozenset({"aarch64", "arm64"})
_MACOS_X64_MACHINES = frozenset({"amd64", "x86_64"})
_WSL_ENVIRONMENT_KEYS = ("WSL_DISTRO_NAME", "WSL_INTEROP")


def detect_host_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    release: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> HostPlatform:
    """Classify the current or injected operating-system host."""
    host_system = platform.system() if system is None else system
    host_machine = platform.machine() if machine is None else machine
    host_release = platform.release() if release is None else release
    source = os.environ if environment is None else environment
    if host_system == "Linux":
        return (
            HostPlatform.WSL
            if is_wsl(host_release, source)
            else HostPlatform.LINUX
        )
    if host_system == "Darwin":
        normalized = host_machine.casefold()
        if normalized in _MACOS_ARM64_MACHINES:
            return HostPlatform.MACOS_ARM64
        if normalized in _MACOS_X64_MACHINES:
            return HostPlatform.MACOS_X64
        return HostPlatform.UNSUPPORTED
    if host_system == "Windows":
        return HostPlatform.WINDOWS
    return HostPlatform.UNSUPPORTED


def is_wsl(
    release: str,
    environment: Mapping[str, str],
) -> bool:
    """Return whether Linux evidence identifies WSL."""
    return "microsoft" in release.casefold() or any(
        environment.get(name) for name in _WSL_ENVIRONMENT_KEYS
    )
