"""Codex executable discovery and immutable provenance checks."""

import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexExecutable,
    CodexVersion,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
    run_bounded_codex_command,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

SUPPORTED_CODEX_VERSION = CodexVersion(0, 145, 0)
_CODEX_COMMAND = "codex"
_VERSION_OUTPUT_BYTES = 128
_VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_PATTERN = re.compile(
    r"codex-cli (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
)


def discover_codex_executable(
    environment: Mapping[str, str] | None = None,
) -> CodexExecutable:
    """Resolve, version, and freeze one exact Codex executable."""
    source = os.environ if environment is None else environment
    candidate = shutil.which(
        _CODEX_COMMAND,
        path=source.get("PATH", os.defpath),
    )
    if candidate is None:
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_MISSING)
    unresolved = Path(candidate)
    if not unresolved.is_absolute():
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_UNSAFE)
    try:
        path = unresolved.resolve(strict=True)
        before = path.stat()
    except OSError:
        raise CodexAppServerError(
            CodexAppServerFailure.EXECUTABLE_UNSAFE
        ) from None
    if not stat.S_ISREG(before.st_mode) or not os.access(path, os.X_OK):
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_UNSAFE)
    output = run_bounded_codex_command(
        (str(path), "--version"),
        minimal_codex_environment(source),
        timeout_seconds=_VERSION_TIMEOUT_SECONDS,
        maximum_output_bytes=_VERSION_OUTPUT_BYTES,
    )
    version = _parse_version(output)
    if version != SUPPORTED_CODEX_VERSION:
        raise CodexAppServerError(CodexAppServerFailure.VERSION_UNSUPPORTED)
    try:
        after = path.stat()
    except OSError:
        raise CodexAppServerError(
            CodexAppServerFailure.EXECUTABLE_UNSAFE
        ) from None
    if _file_identity(before) != _file_identity(after):
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_UNSAFE)
    return CodexExecutable(
        path=path,
        version=version,
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        modified_nanoseconds=after.st_mtime_ns,
    )


def verify_codex_executable(executable: CodexExecutable) -> None:
    """Require an executable to retain its discovered file identity."""
    try:
        current = executable.path.stat()
    except OSError:
        raise CodexAppServerError(
            CodexAppServerFailure.EXECUTABLE_UNSAFE
        ) from None
    expected = (
        executable.device,
        executable.inode,
        executable.size,
        executable.modified_nanoseconds,
    )
    if (
        _file_identity(current) != expected
        or not stat.S_ISREG(current.st_mode)
        or not os.access(executable.path, os.X_OK)
    ):
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_UNSAFE)


def _parse_version(payload: bytes) -> CodexVersion:
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise CodexAppServerError(
            CodexAppServerFailure.VERSION_UNSUPPORTED
        ) from None
    matched = _VERSION_PATTERN.fullmatch(text)
    if matched is None:
        raise CodexAppServerError(CodexAppServerFailure.VERSION_UNSUPPORTED)
    return CodexVersion(
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
    )


def _file_identity(
    file_status: os.stat_result,
) -> tuple[int, int, int, int]:
    return (
        file_status.st_dev,
        file_status.st_ino,
        file_status.st_size,
        file_status.st_mtime_ns,
    )
