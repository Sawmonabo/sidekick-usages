"""Codex executable discovery and immutable provenance checks."""

import os
import re
from collections.abc import Callable, Mapping

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    resolve_executable,
    verify_executable,
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

SUPPORTED_CODEX_VERSION = CodexVersion(0, 145, 0)
_CODEX_COMMAND = "codex"
_VERSION_OUTPUT_BYTES = 128
_VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_PATTERN = re.compile(
    r"codex-cli (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
)


def discover_codex_executable(
    environment: Mapping[str, str] | None = None,
    *,
    process_group: CodexProcessGroupPolicy = (
        CodexProcessGroupPolicy.ISOLATED
    ),
    cancelled: Callable[[], bool] | None = None,
) -> CodexExecutable:
    """Resolve, version, and freeze one exact Codex executable."""
    source = os.environ if environment is None else environment
    try:
        provenance = resolve_executable(_CODEX_COMMAND, source)
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
    if version != SUPPORTED_CODEX_VERSION:
        raise CodexAppServerError(CodexAppServerFailure.VERSION_UNSUPPORTED)
    try:
        verify_executable(provenance)
    except ExecutableQualificationError:
        raise CodexAppServerError(
            CodexAppServerFailure.EXECUTABLE_UNSAFE
        ) from None
    return CodexExecutable(
        provenance=provenance,
        version=version,
    )


def verify_codex_executable(executable: CodexExecutable) -> None:
    """Require an executable to retain its discovered file identity."""
    try:
        verify_executable(executable.provenance)
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
    return CodexVersion(
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
    )
