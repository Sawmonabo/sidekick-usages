"""Read-only owner qualification for the local control endpoint."""

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from sidekick_usages.daemon.types.lifecycle import ServiceComponentState

RUNTIME_DIRECTORY_MODE = 0o700
SOCKET_MODE = 0o600


def control_endpoint_state(
    runtime_directory: Path,
    socket_path: Path,
) -> ServiceComponentState:
    """Observe exact owner-only endpoint state without changing it."""
    if sys.platform == "win32":
        return ServiceComponentState.FEATURE_DISABLED
    if socket_path.parent != runtime_directory:
        return ServiceComponentState.UNHEALTHY
    runtime_state = _owned_path_state(
        runtime_directory,
        runtime_directory_owned,
    )
    if runtime_state is not ServiceComponentState.HEALTHY:
        return runtime_state
    return _owned_path_state(socket_path, socket_owned)


def runtime_directory_owned(metadata: os.stat_result) -> bool:
    """Return whether metadata proves the exact runtime-directory policy."""
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == RUNTIME_DIRECTORY_MODE
    )


def socket_owned(metadata: os.stat_result) -> bool:
    """Return whether metadata proves the exact control-socket policy."""
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == SOCKET_MODE
    )


def _owned_path_state(
    path: Path,
    ownership_check: Callable[[os.stat_result], bool],
) -> ServiceComponentState:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ServiceComponentState.ABSENT
    except OSError:
        return ServiceComponentState.UNHEALTHY
    if ownership_check(metadata):
        return ServiceComponentState.HEALTHY
    return ServiceComponentState.UNHEALTHY
