"""Exact managed Claude executable discovery."""

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
    resolve_executable,
    verify_executable,
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

SUPPORTED_CLAUDE_VERSION = ClaudeVersion(2, 1, 220)
_CLAUDE_COMMAND = "claude"
_VERSION_OUTPUT_BYTES = 128
_VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_PATTERN = re.compile(
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+) "
    r"\(Claude Code\)"
)


def discover_claude_executable(
    environment: Mapping[str, str] | None = None,
    *,
    executable_path: Path | None = None,
    working_directory: Path | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
    cancelled: Callable[[], bool] | None = None,
) -> ClaudeExecutable:
    """Resolve, version, and freeze one exact Claude executable."""
    source = os.environ if environment is None else environment
    try:
        provenance = (
            resolve_claude_executable(source)
            if executable_path is None
            else qualify_executable(executable_path)
        )
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
    if version != SUPPORTED_CLAUDE_VERSION:
        raise ClaudeManagedError(ClaudeManagedFailure.VERSION_UNSUPPORTED)
    _verify_provenance(provenance)
    return ClaudeExecutable(provenance, version)


def discover_pinned_claude_executable(
    executable_path: Path | None,
    environment: Mapping[str, str] | None = None,
    *,
    working_directory: Path | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
    cancelled: Callable[[], bool] | None = None,
) -> ClaudeExecutable:
    """Discover only the service-pinned Claude executable."""
    if executable_path is None:
        raise ClaudeManagedError(ClaudeManagedFailure.EXECUTABLE_MISSING)
    return discover_claude_executable(
        environment,
        executable_path=executable_path,
        working_directory=working_directory,
        runner=runner,
        cancelled=cancelled,
    )


def resolve_claude_executable(
    environment: Mapping[str, str] | None = None,
) -> ExecutableProvenance:
    """Resolve one qualified Claude executable without running it."""
    source = os.environ if environment is None else environment
    return resolve_executable(_CLAUDE_COMMAND, source)


def verify_claude_executable(executable: ClaudeExecutable) -> None:
    """Require the exact discovered Claude executable to remain."""
    _verify_provenance(executable.provenance)


def _verify_provenance(provenance: ExecutableProvenance) -> None:
    try:
        verify_executable(provenance)
    except ExecutableQualificationError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.EXECUTABLE_UNSAFE
        ) from None


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
