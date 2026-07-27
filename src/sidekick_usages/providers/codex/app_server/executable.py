"""Codex executable discovery and immutable provenance checks."""

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
from sidekick_usages.platform.types import ExecutableFailure
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
    CodexProcessGroupPolicy,
)

MINIMUM_CODEX_VERSION = CodexVersion(0, 145, 0)
_CODEX_COMMAND = "codex"
_VERSION_OUTPUT_BYTES = 128
_VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_PATTERN = re.compile(r"codex-cli (?P<version>\d+\.\d+\.\d+)")


def discover_codex_executable(
    environment: Mapping[str, str] | None = None,
    *,
    launcher: Path | None = None,
    process_group: CodexProcessGroupPolicy = (
        CodexProcessGroupPolicy.ISOLATED
    ),
    cancelled: Callable[[], bool] | None = None,
) -> CodexExecutable:
    """Resolve, version, and freeze one exact Codex executable."""
    source = os.environ if environment is None else environment
    try:
        resolved_launcher = (
            resolve_codex_launcher(source) if launcher is None else launcher
        )
        provenance = qualify_executable(resolved_launcher)
    except ExecutableQualificationError as error:
        failure = (
            CodexAppServerFailure.EXECUTABLE_MISSING
            if error.code is ExecutableFailure.MISSING
            else CodexAppServerFailure.EXECUTABLE_UNSAFE
        )
        raise CodexAppServerError(failure) from None
    output = run_bounded_codex_command(
        (str(provenance.path), "--version"),
        minimal_codex_environment(source),
        timeout_seconds=_VERSION_TIMEOUT_SECONDS,
        maximum_output_bytes=_VERSION_OUTPUT_BYTES,
        process_group=process_group,
        cancelled=cancelled,
    )
    version = _parse_version(output)
    if version < MINIMUM_CODEX_VERSION:
        raise CodexAppServerError(CodexAppServerFailure.VERSION_UNSUPPORTED)
    executable = CodexExecutable(
        launcher=resolved_launcher,
        provenance=provenance,
        version=version,
    )
    verify_codex_executable(executable)
    return executable


def discover_codex_executable_from_launcher(
    launcher: Path | None,
    environment: Mapping[str, str] | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> CodexExecutable:
    """Discover the current target of one service-selected launcher."""
    if launcher is None:
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_MISSING)
    return discover_codex_executable(
        environment,
        launcher=launcher,
        cancelled=cancelled,
    )


def resolve_codex_launcher(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one stable qualified Codex launcher without running it."""
    source = os.environ if environment is None else environment
    return resolve_executable_launcher(_CODEX_COMMAND, source)


def verify_codex_executable(executable: CodexExecutable) -> None:
    """Require the launcher to retain the operation's qualified target."""
    try:
        verify_executable_launcher(
            executable.launcher,
            executable.provenance,
        )
    except ExecutableQualificationError:
        raise CodexAppServerError(
            CodexAppServerFailure.EXECUTABLE_UNSAFE
        ) from None


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
    return CodexVersion.parse(matched.group("version"))
