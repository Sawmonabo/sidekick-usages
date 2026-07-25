"""Exact read-only macOS Keychain boundary for Claude credentials."""

import hashlib
import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
    verify_executable,
)
from sidekick_usages.providers.claude.environment import (
    claude_keychain_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.executable import (
    SUPPORTED_CLAUDE_VERSION,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.managed.storage.models import (
    ClaudeKeychainTarget,
)
from sidekick_usages.providers.claude.managed.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeProcessFailure,
)

KEYCHAIN_CREDENTIAL_BYTES = 1024 * 1024
KEYCHAIN_READ_TIMEOUT_SECONDS = 10.0
_KEYCHAIN_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z", re.ASCII)
_KEYCHAIN_ACCESS_DENIED_EXITS = frozenset(
    {
        (-25293) % 256,
        (-128) % 256,
    }
)
_KEYCHAIN_ACCOUNT_FALLBACK = "claude-code-user"
_KEYCHAIN_FIND_ARGUMENT = "find-generic-password"
_KEYCHAIN_LOCKED_EXIT = (-25308) % 256
_KEYCHAIN_MISSING_EXIT = (-25300) % 256
_KEYCHAIN_NATIVE_SERVICE = "Claude Code-credentials"
_KEYCHAIN_OUTPUT_BYTES = KEYCHAIN_CREDENTIAL_BYTES + 2
_KEYCHAIN_PROFILE_PREFIX = "Claude Code-credentials-"
_KEYCHAIN_SECURITY_EXECUTABLE = Path("/usr/bin/security")
_KEYCHAIN_SUPPORTED_PLATFORMS = frozenset(
    {
        ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
        ClaudeManagedPlatform.MACOS_X64_KEYCHAIN,
    }
)
_KEYCHAIN_NAMESPACE_VERSIONS = frozenset({SUPPORTED_CLAUDE_VERSION})
_SECURE_STORAGE_OVERRIDE = "CLAUDE_SECURESTORAGE_CONFIG_DIR"


def native_keychain_target(
    environment: Mapping[str, str] | None = None,
) -> ClaudeKeychainTarget:
    """Return Claude's exact native Keychain lookup target."""
    return ClaudeKeychainTarget(
        _keychain_account(environment),
        _KEYCHAIN_NATIVE_SERVICE,
    )


def managed_keychain_target(
    capabilities: ClaudeCapabilities,
    environment: Mapping[str, str] | None = None,
) -> ClaudeKeychainTarget:
    """Return one release-proven managed-profile Keychain target."""
    source = os.environ if environment is None else environment
    if (
        capabilities.platform not in _KEYCHAIN_SUPPORTED_PLATFORMS
        or capabilities.executable.version not in _KEYCHAIN_NAMESPACE_VERSIONS
        or _SECURE_STORAGE_OVERRIDE in source
    ):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN
        )
    config_text = str(capabilities.profile.config_directory)
    if unicodedata.normalize("NFC", config_text) != config_text:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN
        )
    suffix = hashlib.sha256(config_text.encode("utf-8")).hexdigest()[:8]
    return ClaudeKeychainTarget(
        _keychain_account(source),
        _KEYCHAIN_PROFILE_PREFIX + suffix,
    )


def read_keychain_payload(
    target: ClaudeKeychainTarget,
    environment: Mapping[str, str] | None = None,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> bytes:
    """Read one bounded Keychain value through the exact system binary."""
    try:
        executable = qualify_executable(_KEYCHAIN_SECURITY_EXECUTABLE)
        verify_executable(executable)
    except ExecutableQualificationError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.UNREADABLE
        ) from None
    if executable.path != _KEYCHAIN_SECURITY_EXECUTABLE:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.UNREADABLE
        )
    try:
        result = runner(
            (
                str(executable.path),
                _KEYCHAIN_FIND_ARGUMENT,
                "-a",
                target.account,
                "-w",
                "-s",
                target.service,
            ),
            timeout_seconds=KEYCHAIN_READ_TIMEOUT_SECONDS,
            maximum_output_bytes=_KEYCHAIN_OUTPUT_BYTES,
            environment=claude_keychain_environment(environment),
        )
    except ClaudeProcessError as error:
        failure = (
            ClaudeProtectedStorageFailure.MALFORMED
            if error.code is ClaudeProcessFailure.OUTPUT_TOO_LARGE
            else ClaudeProtectedStorageFailure.UNREADABLE
        )
        raise ClaudeProtectedStorageError(failure) from None
    try:
        verify_executable(executable)
    except ExecutableQualificationError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.UNREADABLE
        ) from None
    if result.return_code != 0:
        raise ClaudeProtectedStorageError(
            _keychain_failure(result.return_code)
        )
    payload = result.output
    if payload.endswith(b"\n"):
        payload = payload[:-1]
        if payload.endswith(b"\r"):
            payload = payload[:-1]
    if not payload or len(payload) > KEYCHAIN_CREDENTIAL_BYTES:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        )
    return payload


def _keychain_account(
    environment: Mapping[str, str] | None,
) -> str:
    source = os.environ if environment is None else environment
    account = source.get("USER", _KEYCHAIN_ACCOUNT_FALLBACK)
    return (
        account
        if _KEYCHAIN_ACCOUNT_PATTERN.fullmatch(account) is not None
        else _KEYCHAIN_ACCOUNT_FALLBACK
    )


def _keychain_failure(return_code: int) -> ClaudeProtectedStorageFailure:
    if return_code == _KEYCHAIN_MISSING_EXIT:
        return ClaudeProtectedStorageFailure.MISSING
    if return_code == _KEYCHAIN_LOCKED_EXIT:
        return ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED
    if return_code in _KEYCHAIN_ACCESS_DENIED_EXITS:
        return ClaudeProtectedStorageFailure.KEYCHAIN_ACCESS_DENIED
    return ClaudeProtectedStorageFailure.UNREADABLE
