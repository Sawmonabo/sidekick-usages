"""Protected Claude credential authority composition."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
)
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    KnownExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import ClaudeLoginCredentials
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.base import ProviderBoundaryError
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    protected_keychain_target,
    read_keychain_payload,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeCredentialPayload,
    ClaudeProtectedCredential,
    ClaudeProtectedCredentialSnapshot,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeCredentialFileSource,
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.environment import (
    encode_claude_refresh_scopes,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
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

CLAUDE_CREDENTIAL_FILE = ".credentials.json"
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


def read_protected_claude_credential(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeProtectedCredentialSnapshot:
    """Read one exact protected Claude credential snapshot."""
    with protected_claude_credential(
        capabilities,
        files,
        reference_time,
        environment=environment,
        runner=runner,
    ) as protected:
        return protected.snapshot


@contextmanager
def protected_claude_credential(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> Iterator[ClaudeProtectedCredential]:
    """Yield one short-lived credential projection from protected storage."""
    protected = _read_protected_credential(
        capabilities,
        files,
        reference_time,
        environment,
        runner,
    )
    with protected as active:
        yield active


def _read_protected_credential(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> ClaudeProtectedCredential:
    payload = _read_protected_payload(
        capabilities,
        files,
        environment,
        runner,
    )
    snapshot, credentials = _credential_snapshot(
        capabilities,
        payload,
        reference_time,
    )
    return ClaudeProtectedCredential(
        snapshot=snapshot,
        credentials=credentials,
    )


def _read_protected_payload(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> ClaudeCredentialPayload:
    if capabilities.platform in _FILE_PLATFORMS:
        payload = files.read(capabilities.profile)
        if payload is None:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.MISSING
            )
        return payload
    if capabilities.platform in _KEYCHAIN_PLATFORMS:
        return _read_macos_payload(
            capabilities,
            files,
            environment,
            runner,
        )
    raise ClaudeProtectedStorageError(
        ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN
    )


def _read_macos_payload(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> ClaudeCredentialPayload:
    if files.present(capabilities.profile):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
        )
    target = protected_keychain_target(capabilities, environment)
    payload = read_keychain_payload(
        target,
        environment,
        runner=runner,
    )
    if files.present(capabilities.profile):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
        )
    return ClaudeCredentialPayload(payload)


def _credential_snapshot(
    capabilities: ClaudeCapabilities,
    payload: ClaudeCredentialPayload,
    reference_time: datetime,
) -> tuple[ClaudeProtectedCredentialSnapshot, ClaudeLoginCredentials]:
    try:
        detected = parse_credentials_blob(decode_json_object(payload.data))
    except InvalidPayloadError, ProviderBoundaryError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        ) from None
    credentials = detected.credentials
    if not isinstance(credentials, ClaudeLoginCredentials):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        )
    try:
        encode_claude_refresh_scopes(credentials.scopes)
    except ClaudeProcessError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        ) from None
    health, action = _health(credentials, reference_time)
    generation = claude_access_token_generation(credentials.access_token)
    refresh_expiry = credentials.refresh_expiry
    snapshot = ClaudeProtectedCredentialSnapshot(
        profile=capabilities.profile,
        executable_version=str(capabilities.executable.version),
        generation=generation,
        plan=detected.plan,
        scopes=credentials.scopes,
        access_expires_at=credentials.access_expiry.at,
        refresh_expires_at=(
            refresh_expiry.at
            if isinstance(refresh_expiry, KnownExpiry)
            else None
        ),
        health=health,
        action=action,
        modified_nanoseconds=payload.modified_nanoseconds,
    )
    return snapshot, credentials


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
