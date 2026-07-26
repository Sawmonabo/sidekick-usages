"""Claude Code platform credential discovery."""

import os
import platform
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.base import (
    CredentialDetection,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    native_keychain_target,
    read_keychain_payload,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
)
from sidekick_usages.providers.claude.errors import claude_failure
from sidekick_usages.providers.claude.models import ClaudeNativeProfile
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner
from sidekick_usages.serialization.json import decode_json_object

CLAUDE_SETUP_REJECTION_MESSAGE = "Claude rejected the saved setup token."
CLAUDE_SUBSCRIPTION_LOGIN_REJECTED = (
    "Claude rejected the saved subscription login."
)
_MAX_CREDENTIAL_BYTES = 1024 * 1024


def native_claude_profile(
    credential_home: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> ClaudeNativeProfile:
    """Resolve one explicit native Claude configuration profile."""
    source = os.environ if environment is None else environment
    configured = source.get(CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY)
    if configured == "":
        raise ValueError("Claude native profile path is unavailable.")
    try:
        directory = (
            credential_home
            if credential_home is not None
            else (
                Path(configured).expanduser()
                if configured is not None
                else Path.home() / ".claude"
            )
        )
        return ClaudeNativeProfile(directory.resolve(strict=False))
    except OSError, RuntimeError:
        raise ValueError(
            "Claude native profile path is unavailable."
        ) from None


def detect_credentials(
    reference_time: datetime,
    profile: ClaudeNativeProfile,
    *,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> CredentialDetection:
    """Read credentials from one explicit native Claude profile."""
    system = platform.system()
    if system == "Darwin":
        return _from_macos_keychain(
            reference_time,
            environment,
            runner,
        )
    if system in {"Linux", "Windows"}:
        return read_credentials_path(
            profile.config_directory / CLAUDE_CREDENTIAL_FILE,
            reference_time,
        )
    return claude_failure(
        ProviderFailureKind.UNSUPPORTED,
        "Claude credential discovery is unsupported on this platform.",
    )


def require_claude_credentials(account: Account) -> ClaudeCredentials:
    """Return Claude credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(
        credentials,
        ClaudeSetupTokenCredentials | ClaudeLoginCredentials,
    ):
        return credentials
    raise ProviderBoundaryError(
        claude_failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The saved account does not contain Claude credentials.",
        )
    ) from None


def _from_macos_keychain(
    reference_time: datetime,
    environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> CredentialDetection:
    try:
        payload = read_keychain_payload(
            native_keychain_target(environment),
            environment,
            runner=runner,
        )
    except ClaudeProtectedStorageError as error:
        if error.code is ClaudeProtectedStorageFailure.MISSING:
            return _missing_credentials()
        if error.code is ClaudeProtectedStorageFailure.MALFORMED:
            return claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude credential data is malformed.",
            )
        return unreadable_credentials()
    return parse_detected_credentials(payload, reference_time)


def read_credentials_path(
    path: Path,
    reference_time: datetime,
) -> CredentialDetection:
    """Read and classify one concrete Claude credential file."""
    try:
        path.stat()
    except FileNotFoundError:
        return _missing_credentials()
    except OSError:
        return unreadable_credentials()
    try:
        payload = _read_bounded(path)
    except OSError:
        return unreadable_credentials()
    except ValueError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data exceeds the supported size.",
        )
    return parse_detected_credentials(payload, reference_time)


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read(_MAX_CREDENTIAL_BYTES + 1)
    if len(payload) > _MAX_CREDENTIAL_BYTES:
        raise ValueError
    return payload


def parse_detected_credentials(
    payload: bytes,
    reference_time: datetime,
) -> CredentialDetection:
    """Decode, validate, and classify one Claude credential payload."""
    try:
        blob = decode_json_object(payload)
    except InvalidPayloadError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data is not valid JSON.",
        )
    try:
        detected = parse_credentials_blob(blob)
    except ProviderBoundaryError as error:
        return error.failure
    credentials = detected.credentials
    if not isinstance(credentials, ClaudeLoginCredentials):
        raise AssertionError(
            "Claude native parsing returned setup credentials."
        )
    if isinstance(
        classify_expiry(
            credentials.refresh_expiry,
            now=reference_time,
        ),
        ExpiredExpiry,
    ):
        return claude_failure(
            ProviderFailureKind.EXPIRED,
            "The saved Claude login credential has expired.",
            cause=ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED,
        )
    if isinstance(
        classify_expiry(credentials.access_expiry, now=reference_time),
        ExpiredExpiry,
    ):
        return claude_failure(
            ProviderFailureKind.EXPIRED,
            "The saved Claude access credential has expired.",
            cause=ProviderFailureCause.ACCESS_CREDENTIAL_EXPIRED,
        )
    return detected


def _missing_credentials() -> ProviderFailure:
    return claude_failure(
        ProviderFailureKind.MISSING,
        "Claude credentials were not found. Log in with Claude Code.",
    )


def unreadable_credentials() -> ProviderFailure:
    return claude_failure(
        ProviderFailureKind.UNREADABLE,
        "Claude credentials could not be read. Check access and retry.",
    )
