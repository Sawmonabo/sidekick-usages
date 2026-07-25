"""Protected managed-Claude credential authority composition."""

from collections.abc import Mapping
from datetime import datetime

from sidekick_usages.core.accounts.generation import (
    hashed_authority_generation,
)
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
)
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    KnownExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import ClaudeLoginCredentials
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.base import ProviderBoundaryError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.managed.storage.keychain import (
    managed_keychain_target,
    read_keychain_payload,
)
from sidekick_usages.providers.claude.managed.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.storage.types import (
    ClaudeCredentialFileSource,
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner
from sidekick_usages.serialization.json import decode_json_object

_CLAUDE_GENERATION_PREFIX = "claude-access-token-sha256:"
_FILE_PLATFORMS = frozenset(
    {
        ClaudeManagedPlatform.LINUX_FILE,
        ClaudeManagedPlatform.WSL_FILE,
    }
)
_KEYCHAIN_PLATFORMS = frozenset(
    {
        ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
        ClaudeManagedPlatform.MACOS_X64_KEYCHAIN,
    }
)


def read_protected_claude_authority(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    expected_identity: ProviderIdentity | None = None,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeAuthoritySnapshot:
    """Read and bind one exact managed Claude credential authority."""
    if capabilities.platform in _FILE_PLATFORMS:
        payload = files.read(capabilities.profile)
        if payload is None:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.MISSING
            )
    elif capabilities.platform in _KEYCHAIN_PLATFORMS:
        payload = _read_macos_payload(
            capabilities,
            files,
            environment,
            runner,
        )
    else:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN
        )
    return _snapshot(
        capabilities,
        payload,
        reference_time,
        expected_identity,
    )


def _read_macos_payload(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> bytes:
    if files.present(capabilities.profile):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
        )
    target = managed_keychain_target(capabilities, environment)
    payload = read_keychain_payload(
        target,
        environment,
        runner=runner,
    )
    if files.present(capabilities.profile):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
        )
    return payload


def _snapshot(
    capabilities: ClaudeCapabilities,
    payload: bytes,
    reference_time: datetime,
    expected_identity: ProviderIdentity | None,
) -> ClaudeAuthoritySnapshot:
    try:
        detected = parse_credentials_blob(decode_json_object(payload))
    except InvalidPayloadError, ProviderBoundaryError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        ) from None
    credentials = detected.credentials
    if not isinstance(credentials, ClaudeLoginCredentials):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        )
    identity = credentials.identity
    if identity is None:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        )
    provider_identity = identity.provider_identity
    if (
        expected_identity is not None
        and provider_identity != expected_identity
    ):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
        )
    health, action = _health(credentials, reference_time)
    refresh_expiry = credentials.refresh_expiry
    return ClaudeAuthoritySnapshot(
        profile=capabilities.profile,
        executable_version=str(capabilities.executable.version),
        provider_identity=provider_identity,
        generation=hashed_authority_generation(
            credentials.access_token,
            prefix=_CLAUDE_GENERATION_PREFIX,
        ),
        plan=detected.plan,
        access_expires_at=credentials.access_expiry.at,
        refresh_expires_at=(
            refresh_expiry.at
            if isinstance(refresh_expiry, KnownExpiry)
            else None
        ),
        health=health,
        action=action,
    )


def _health(
    credentials: ClaudeLoginCredentials,
    reference_time: datetime,
) -> tuple[CredentialHealth, CredentialAction]:
    if isinstance(
        classify_expiry(credentials.refresh_expiry, now=reference_time),
        ExpiredExpiry,
    ):
        return CredentialHealth.LOGIN_REQUIRED, CredentialAction.LOGIN
    if isinstance(
        classify_expiry(credentials.access_expiry, now=reference_time),
        ExpiredExpiry,
    ):
        return CredentialHealth.REFRESH_DUE, CredentialAction.REFRESH
    return CredentialHealth.HEALTHY, CredentialAction.NONE
