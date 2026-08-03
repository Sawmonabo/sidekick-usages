"""Exact managed Claude executable discovery."""

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
    resolve_executable_launcher,
    verify_executable_launcher,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import ExecutableFailure
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import (
    ClaudeManagedError,
    raise_managed_capability_error,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeVersion,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

MINIMUM_CLAUDE_VERSION = ClaudeVersion(2, 1, 220)
_CLAUDE_COMMAND = "claude"
_VERSION_OUTPUT_BYTES = 128
_VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_PATTERN = re.compile(
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+) "
    r"\(Claude Code\)"
)
_ARTIFACT_READ_BYTES = 1024 * 1024
_MAXIMUM_ARTIFACT_MARKER_BYTES = 256


def discover_claude_executable(
    environment: Mapping[str, str] | None = None,
    *,
    launcher: Path | None = None,
    working_directory: Path | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
    cancelled: Callable[[], bool] | None = None,
) -> ClaudeExecutable:
    """Resolve, version, and freeze one exact Claude executable."""
    source = os.environ if environment is None else environment
    try:
        resolved_launcher = (
            resolve_claude_launcher(source) if launcher is None else launcher
        )
        provenance = qualify_executable(resolved_launcher)
    except ExecutableQualificationError as error:
        failure = (
            ClaudeManagedFailure.EXECUTABLE_MISSING
            if error.code is ExecutableFailure.MISSING
            else ClaudeManagedFailure.EXECUTABLE_UNSAFE
        )
        raise ClaudeManagedError(failure) from None
    try:
        result = runner(
            (str(provenance.path), "--version"),
            timeout_seconds=_VERSION_TIMEOUT_SECONDS,
            maximum_output_bytes=_VERSION_OUTPUT_BYTES,
            environment=source,
            working_directory=working_directory,
            cancelled=cancelled,
        )
    except ClaudeProcessError as error:
        raise_managed_capability_error(
            error,
            ClaudeManagedFailure.EXECUTABLE_UNSAFE,
        )
    if result.return_code != 0:
        raise ClaudeManagedError(ClaudeManagedFailure.VERSION_UNSUPPORTED)
    version = _parse_version(result.output)
    if version < MINIMUM_CLAUDE_VERSION:
        raise ClaudeManagedError(ClaudeManagedFailure.VERSION_UNSUPPORTED)
    executable = ClaudeExecutable(resolved_launcher, provenance, version)
    verify_claude_executable(executable)
    return executable


def discover_claude_executable_from_launcher(
    launcher: Path | None,
    environment: Mapping[str, str] | None = None,
    *,
    working_directory: Path | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
    cancelled: Callable[[], bool] | None = None,
) -> ClaudeExecutable:
    """Discover the current target of one service-selected launcher."""
    if launcher is None:
        raise ClaudeManagedError(ClaudeManagedFailure.EXECUTABLE_MISSING)
    return discover_claude_executable(
        environment,
        launcher=launcher,
        working_directory=working_directory,
        runner=runner,
        cancelled=cancelled,
    )


def resolve_claude_launcher(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one stable qualified Claude launcher without running it."""
    source = os.environ if environment is None else environment
    return resolve_executable_launcher(_CLAUDE_COMMAND, source)


def verify_claude_executable(executable: ClaudeExecutable) -> None:
    """Require the launcher to retain the operation's qualified target."""
    try:
        verify_executable_launcher(
            executable.launcher,
            executable.provenance,
        )
    except ExecutableQualificationError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.EXECUTABLE_UNSAFE
        ) from None


def inspect_claude_executable_artifact(
    executable: ClaudeExecutable,
    markers: tuple[bytes, ...],
) -> tuple[str, frozenset[bytes]]:
    """Hash one frozen executable and locate bounded build markers."""
    if (
        not markers
        or len(markers) != len(set(markers))
        or any(
            not marker or len(marker) > _MAXIMUM_ARTIFACT_MARKER_BYTES
            for marker in markers
        )
    ):
        raise ValueError("Claude executable markers are invalid.")
    verify_claude_executable(executable)
    digest = hashlib.sha256()
    found: set[bytes] = set()
    overlap = max(len(marker) for marker in markers) - 1
    tail = b""
    try:
        with executable.provenance.path.open("rb") as stream:
            initial = ExecutableProvenance.from_stat(
                executable.provenance.path,
                os.fstat(stream.fileno()),
            )
            if initial != executable.provenance:
                raise OSError
            while chunk := stream.read(_ARTIFACT_READ_BYTES):
                digest.update(chunk)
                window = tail + chunk
                found.update(marker for marker in markers if marker in window)
                tail = window[-overlap:] if overlap else b""
            final = ExecutableProvenance.from_stat(
                executable.provenance.path,
                os.fstat(stream.fileno()),
            )
            if final != initial:
                raise OSError
    except OSError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.EXECUTABLE_UNSAFE
        ) from None
    verify_claude_executable(executable)
    return digest.hexdigest(), frozenset(found)


def _parse_version(payload: bytes) -> ClaudeVersion:
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.VERSION_UNSUPPORTED
        ) from None
    matched = _VERSION_PATTERN.fullmatch(text)
    if matched is None:
        raise ClaudeManagedError(ClaudeManagedFailure.VERSION_UNSUPPORTED)
    return ClaudeVersion(
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
    )
