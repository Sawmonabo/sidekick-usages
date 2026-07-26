"""Codex auth discovery, identity, and protected managed-home reading."""

import re
import tomllib
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.selection.models import ProviderAuthObservation
from sidekick_usages.core.selection.types import ProviderAuthState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import InvalidPayloadError, UsageError
from sidekick_usages.providers.base import (
    CredentialDetection,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.auth.generation import (
    codex_generation_order,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.auth.models import CodexAuthSnapshot
from sidekick_usages.providers.codex.auth.token import (
    codex_access_token_generation,
)
from sidekick_usages.providers.codex.failures import codex_failure
from sidekick_usages.providers.codex.schema.auth import parse_auth_credentials
from sidekick_usages.serialization.json import JsonObject, decode_json_object

CODEX_AUTH_FILE = "auth.json"
CODEX_CONFIG_FILE = "config.toml"
CODEX_FILE_AUTH_CONFIG = 'cli_auth_credentials_store = "file"'
_MAX_CODEX_FILE_BYTES = 1024 * 1024
_NATIVE_AUTH_FAILURE_STATES = {
    ProviderFailureKind.MISSING: ProviderAuthState.LOGGED_OUT,
    ProviderFailureKind.UNSUPPORTED: ProviderAuthState.UNSUPPORTED,
}


def _codex_auth_path(credential_home: Path | None = None) -> Path:
    """Return the auth.json path for a Codex home or auth file path."""
    home = default_codex_home() if credential_home is None else credential_home
    home = home.expanduser()
    if home.name == CODEX_AUTH_FILE:
        return home
    return home / CODEX_AUTH_FILE


def _read_auth_blob(
    credential_home: Path | None = None,
) -> JsonObject | ProviderFailure:
    """Read auth.json or return one explicit safe source failure."""
    path = _codex_auth_path(credential_home)
    try:
        payload = _read_bounded(path)
    except FileNotFoundError:
        return codex_failure(
            ProviderFailureKind.MISSING,
            "No Codex auth.json was found; run `codex login` first.",
        )
    except OSError:
        return codex_failure(
            ProviderFailureKind.UNREADABLE,
            "Codex auth.json could not be read; check its permissions.",
        )
    if len(payload) > _MAX_CODEX_FILE_BYTES:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json exceeds the supported size; log in again.",
        )
    try:
        return decode_json_object(payload)
    except InvalidPayloadError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json is not valid JSON; run `codex login` again.",
        )


def parse_managed_auth_snapshot(
    auth_payload: bytes | None,
    config_payload: bytes | None,
) -> CodexAuthSnapshot | ProviderFailure:
    """Return only identity and generation from one file-backed home."""
    detected = parse_managed_auth_credentials(auth_payload, config_payload)
    if isinstance(detected, ProviderFailure):
        return detected
    return managed_auth_snapshot(detected)


def parse_managed_auth_credentials(
    auth_payload: bytes | None,
    config_payload: bytes | None,
) -> DetectedCredentials | ProviderFailure:
    """Strictly decode one complete file-backed managed authority."""
    config_failure = _file_auth_config_failure(config_payload)
    if config_failure is not None:
        return config_failure
    if auth_payload is None:
        return codex_failure(
            ProviderFailureKind.MISSING,
            "The managed Codex home is logged out.",
        )
    try:
        blob = decode_json_object(auth_payload)
        detected = parse_auth_credentials(blob)
    except InvalidPayloadError, ProviderBoundaryError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex auth state is malformed.",
        )
    return detected


def managed_auth_snapshot(
    detected: DetectedCredentials,
) -> CodexAuthSnapshot | ProviderFailure:
    """Return protected identity metadata from decoded managed credentials."""
    credentials = detected.credentials
    if (
        not isinstance(credentials, CodexCredentials)
        or credentials.account_id is None
        or credentials.auth_last_refresh is None
    ):
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex auth state is incomplete.",
        )
    try:
        order = codex_generation_order(credentials.auth_last_refresh)
    except ValueError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex credential generation is malformed.",
        )
    try:
        return CodexAuthSnapshot(
            provider_identity=ProviderIdentity(credentials.account_id),
            generation=AuthorityGeneration(credentials.auth_last_refresh),
            generation_order=order,
            plan=detected.plan,
        )
    except TypeError, ValueError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex auth metadata is malformed.",
        )


def _detect_auth_credentials(
    credential_home: Path | None = None,
) -> CredentialDetection:
    """Read and validate one Codex auth source."""
    blob = _read_auth_blob(credential_home)
    if isinstance(blob, ProviderFailure):
        return blob
    try:
        return parse_auth_credentials(blob)
    except ProviderBoundaryError as error:
        return error.failure


def observe_native_auth(
    *,
    credential_home: Path | None = None,
    observed_at: datetime,
) -> ProviderAuthObservation:
    """Read native Codex authentication and immediately discard credentials."""
    config_failure = _native_config_failure(credential_home)
    if config_failure is not None:
        return _native_auth_failure(config_failure, observed_at)
    detected = _detect_auth_credentials(credential_home)
    if isinstance(detected, ProviderFailure):
        return _native_auth_failure(detected, observed_at)
    snapshot = managed_auth_snapshot(detected)
    if isinstance(snapshot, ProviderFailure):
        return _native_auth_failure(snapshot, observed_at)
    credentials = detected.credentials
    if not isinstance(credentials, CodexCredentials):
        return _native_auth_failure(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "The native Codex credentials are malformed.",
            ),
            observed_at,
        )
    return ProviderAuthObservation(
        provider_id=ProviderId.CODEX,
        state=ProviderAuthState.ACTIVE,
        provider_identity=snapshot.provider_identity,
        generation=codex_access_token_generation(credentials.access_token),
        observed_at=observed_at,
    )


def _native_config_failure(
    credential_home: Path | None,
) -> ProviderFailure | None:
    path = _codex_auth_path(credential_home).with_name(CODEX_CONFIG_FILE)
    try:
        payload = _read_bounded(path)
    except FileNotFoundError:
        return None
    except OSError:
        return codex_failure(
            ProviderFailureKind.UNREADABLE,
            "The native Codex config could not be read.",
        )
    if len(payload) > _MAX_CODEX_FILE_BYTES:
        return codex_failure(
            ProviderFailureKind.UNREADABLE,
            "The native Codex config exceeds the supported size.",
        )
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, tomllib.TOMLDecodeError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The native Codex config is malformed.",
        )
    storage = document.get("cli_auth_credentials_store")
    if storage is None or storage == "file":
        return None
    return codex_failure(
        ProviderFailureKind.UNSUPPORTED,
        "The native Codex credential store is not file-backed.",
    )


def _native_auth_failure(
    failure: ProviderFailure,
    observed_at: datetime,
) -> ProviderAuthObservation:
    return ProviderAuthObservation(
        provider_id=ProviderId.CODEX,
        state=_NATIVE_AUTH_FAILURE_STATES.get(
            failure.kind,
            ProviderAuthState.UNREADABLE,
        ),
        provider_identity=None,
        generation=None,
        observed_at=observed_at,
    )


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(_MAX_CODEX_FILE_BYTES + 1)


def prepare_file_auth_config(
    existing: bytes | None,
) -> bytes | ProviderFailure:
    """Return a file-backed Codex config without writing provider state."""
    line = CODEX_FILE_AUTH_CONFIG
    if existing is None:
        return f"{line}\n".encode()
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The target Codex config.toml is not valid UTF-8.",
        )
    config_re = re.compile(
        r"(?m)^[ \t]*cli_auth_credentials_store[ \t]*=[ \t]*"
        r'"[^"]*"[ \t]*$'
    )
    if config_re.search(text):
        updated = config_re.sub(line, text, count=1)
    else:
        updated = text.rstrip() + f"\n{line}\n"
    return updated.encode()


def _file_auth_config_failure(
    payload: bytes | None,
) -> ProviderFailure | None:
    if payload is None:
        return codex_failure(
            ProviderFailureKind.UNSUPPORTED,
            "The managed Codex home is not configured for file auth.",
        )
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, tomllib.TOMLDecodeError:
        return codex_failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex config is malformed.",
        )
    if document.get("cli_auth_credentials_store") != "file":
        return codex_failure(
            ProviderFailureKind.UNSUPPORTED,
            "The managed Codex home is not configured for file auth.",
        )
    return None


def require_codex_credentials(account: Account) -> CodexCredentials:
    """Return Codex credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(credentials, CodexCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Codex account.")
