"""Protected Claude credential authority composition."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

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
    ClaudeAuthoritySnapshot,
    ClaudeCredentialObservation,
    ClaudeProtectedLogin,
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


def read_protected_claude_authority(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    expected_identity: ProviderIdentity | None = None,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeAuthoritySnapshot:
    """Read and bind one exact Claude credential authority."""
    with protected_claude_login(
        capabilities,
        files,
        reference_time,
        expected_identity=expected_identity,
        environment=environment,
        runner=runner,
    ) as protected:
        return protected.snapshot


def observe_protected_claude_authority(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeCredentialObservation:
    """Retain real generation without requiring embedded identity."""
    payload = _read_protected_payload(
        capabilities,
        files,
        environment,
        runner,
    )
    observation, credentials = _credential_observation(
        capabilities,
        payload,
        reference_time,
    )
    del credentials
    return observation


@contextmanager
def protected_claude_login(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    expected_identity: ProviderIdentity | None = None,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> Iterator[ClaudeProtectedLogin]:
    """Yield one short-lived refresh projection from protected storage."""
    protected = _read_protected_claude_login(
        capabilities,
        files,
        reference_time,
        expected_identity,
        environment,
        runner,
    )
    with protected as active:
        yield active


def _read_protected_claude_login(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    expected_identity: ProviderIdentity | None,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> ClaudeProtectedLogin:
    payload = _read_protected_payload(
        capabilities,
        files,
        environment,
        runner,
    )
    observation, credentials = _credential_observation(
        capabilities,
        payload,
        reference_time,
    )
    snapshot = observation.snapshot
    if snapshot is None:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        )
    if (
        expected_identity is not None
        and snapshot.provider_identity != expected_identity
    ):
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
        )
    return ClaudeProtectedLogin(snapshot=snapshot, credentials=credentials)


def _read_protected_payload(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> bytes:
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
) -> bytes:
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
    return payload


def _credential_observation(
    capabilities: ClaudeCapabilities,
    payload: bytes,
    reference_time: datetime,
) -> tuple[ClaudeCredentialObservation, ClaudeLoginCredentials]:
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
    try:
        encode_claude_refresh_scopes(credentials.scopes)
    except ClaudeProcessError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.MALFORMED
        ) from None
    health, action = _health(credentials, reference_time)
    generation = claude_access_token_generation(credentials.access_token)
    identity = credentials.identity
    if identity is None:
        return (
            ClaudeCredentialObservation(
                generation=generation,
                health=health,
                action=action,
            ),
            credentials,
        )
    refresh_expiry = credentials.refresh_expiry
    snapshot = ClaudeAuthoritySnapshot(
        profile=capabilities.profile,
        executable_version=str(capabilities.executable.version),
        provider_identity=identity.provider_identity,
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
    )
    return (
        ClaudeCredentialObservation(
            generation=generation,
            health=health,
            action=action,
            snapshot=snapshot,
        ),
        credentials,
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
