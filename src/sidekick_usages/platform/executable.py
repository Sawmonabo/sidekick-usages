"""Provider-neutral exact executable qualification."""

import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import ExecutableFailure


def resolve_executable(
    command: str,
    environment: Mapping[str, str],
) -> ExecutableProvenance:
    """Resolve and freeze one absolute regular executable."""
    candidate = shutil.which(
        command,
        path=environment.get("PATH", os.defpath),
    )
    if candidate is None:
        raise ExecutableQualificationError(ExecutableFailure.MISSING)
    unresolved = Path(candidate)
    if not unresolved.is_absolute():
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)
    try:
        path = unresolved.resolve(strict=True)
        file_status = path.stat()
    except OSError, RuntimeError:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE) from None
    if not stat.S_ISREG(file_status.st_mode) or not os.access(path, os.X_OK):
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)
    return ExecutableProvenance.from_stat(path, file_status)


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
