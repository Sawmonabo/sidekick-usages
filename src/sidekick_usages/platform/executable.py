"""Provider-neutral exact executable qualification."""

import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import ExecutableFailure


def resolve_executable_launcher(
    command: str,
    environment: Mapping[str, str],
) -> Path:
    """Resolve a stable launcher and qualify its current target."""
    candidate = shutil.which(
        command,
        path=environment.get("PATH", os.defpath),
    )
    if candidate is None:
        raise ExecutableQualificationError(ExecutableFailure.MISSING)
    launcher = Path(candidate)
    qualify_executable(launcher)
    return launcher


def qualify_executable(path: Path) -> ExecutableProvenance:
    """Freeze one explicitly located absolute regular executable."""
    if not path.is_absolute():
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)
    try:
        resolved = path.resolve(strict=True)
        file_status = resolved.stat()
    except OSError, RuntimeError:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE) from None
    if not stat.S_ISREG(file_status.st_mode) or not os.access(
        resolved,
        os.X_OK,
    ):
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)
    return ExecutableProvenance.from_stat(resolved, file_status)


def verify_executable(provenance: ExecutableProvenance) -> None:
    """Require one executable to retain its qualified identity."""
    try:
        file_status = provenance.path.stat()
    except OSError:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE) from None
    if (
        ExecutableProvenance.from_stat(provenance.path, file_status)
        != provenance
        or not stat.S_ISREG(file_status.st_mode)
        or not os.access(provenance.path, os.X_OK)
    ):
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)


def verify_executable_launcher(
    launcher: Path,
    provenance: ExecutableProvenance,
) -> None:
    """Require a launcher to retain one qualified operation target."""
    current = qualify_executable(launcher)
    verify_executable(provenance)
    if current != provenance:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)
