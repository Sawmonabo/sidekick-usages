"""Codex auth discovery, identity, login, and pure bundle preparation."""

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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
from sidekick_usages.providers.codex.generation import codex_generation_order
from sidekick_usages.providers.codex.models import CodexAuthSnapshot
from sidekick_usages.providers.codex.native import default_codex_home
from sidekick_usages.providers.codex.schemas import (
    account_id_from_token,
    auth_blob_access_token,
    auth_blob_account_id,
    parse_auth_credentials,
)
from sidekick_usages.serialization.json import JsonObject, decode_json_object

CODEX_AUTH_FILE = "auth.json"
CODEX_CONFIG_FILE = "config.toml"
CODEX_FILE_AUTH_CONFIG = 'cli_auth_credentials_store = "file"'
_MAX_CODEX_FILE_BYTES = 1024 * 1024
_NATIVE_AUTH_FAILURE_STATES = {
    ProviderFailureKind.MISSING: ProviderAuthState.LOGGED_OUT,
    ProviderFailureKind.UNSUPPORTED: ProviderAuthState.UNSUPPORTED,
}


@dataclass(frozen=True, slots=True)
class PreparedCodexAuthBundle:
    """Pure encoded Codex bundle awaiting credential coordination."""

    bundle_path: Path
    files: tuple[tuple[str, bytes], ...] = field(repr=False)
    credentials: CodexCredentials = field(repr=False)

    def file_map(self) -> dict[str, bytes]:
        """Return a fresh mapping for the persistence writer."""
        return dict(self.files)


def _failure(kind: ProviderFailureKind, message: str) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )


def codex_auth_path(credential_home: Path | None = None) -> Path:
    """Return the auth.json path for a Codex home or auth file path."""
    home = default_codex_home() if credential_home is None else credential_home
    home = home.expanduser()
    if home.name == CODEX_AUTH_FILE:
        return home
    return home / CODEX_AUTH_FILE


def read_auth_blob(
    credential_home: Path | None = None,
) -> JsonObject | ProviderFailure:
    """Read auth.json or return one explicit safe source failure."""
    path = codex_auth_path(credential_home)
    try:
        payload = _read_bounded(path)
    except FileNotFoundError:
        return _failure(
            ProviderFailureKind.MISSING,
            "No Codex auth.json was found; run `codex login` first.",
        )
    except OSError:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "Codex auth.json could not be read; check its permissions.",
        )
    if len(payload) > _MAX_CODEX_FILE_BYTES:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json exceeds the supported size; log in again.",
        )
    try:
        return decode_json_object(payload)
    except InvalidPayloadError:
        return _failure(
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
        return _failure(
            ProviderFailureKind.MISSING,
            "The managed Codex home is logged out.",
        )
    try:
        blob = decode_json_object(auth_payload)
        detected = parse_auth_credentials(blob)
    except InvalidPayloadError, ProviderBoundaryError:
        return _failure(
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
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex auth state is incomplete.",
        )
    try:
        order = codex_generation_order(credentials.auth_last_refresh)
    except ValueError:
        return _failure(
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
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex auth metadata is malformed.",
        )


def detect_auth_credentials(
    credential_home: Path | None = None,
) -> CredentialDetection:
    """Read and validate one Codex auth source."""
    blob = read_auth_blob(credential_home)
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
    detected = detect_auth_credentials(credential_home)
    if isinstance(detected, ProviderFailure):
        return _native_auth_failure(detected, observed_at)
    snapshot = managed_auth_snapshot(detected)
    if isinstance(snapshot, ProviderFailure):
        return _native_auth_failure(snapshot, observed_at)
    return ProviderAuthObservation(
        provider_id=ProviderId.CODEX,
        state=ProviderAuthState.ACTIVE,
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        observed_at=observed_at,
    )


def _native_config_failure(
    credential_home: Path | None,
) -> ProviderFailure | None:
    path = codex_auth_path(credential_home).with_name(CODEX_CONFIG_FILE)
    try:
        payload = _read_bounded(path)
    except FileNotFoundError:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "The native Codex credential store could not be resolved.",
        )
    except OSError:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "The native Codex config could not be read.",
        )
    if len(payload) > _MAX_CODEX_FILE_BYTES:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "The native Codex config exceeds the supported size.",
        )
    return _file_auth_config_failure(payload)


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


def prepare_private_bundle(
    account: Account,
    bundle_path: Path,
    *,
    source_home: Path | None,
    reference_time: datetime,
) -> PreparedCodexAuthBundle | ProviderFailure:
    """Purely encode a private Codex bundle without writing or mutation."""
    source = read_auth_blob(source_home)
    if isinstance(source, ProviderFailure):
        if source.kind is ProviderFailureKind.MISSING:
            source_blob: JsonObject = {}
        else:
            return source
    else:
        source_blob = source
        try:
            matches = auth_blob_matches_account(source_blob, account)
        except ProviderBoundaryError as error:
            return error.failure
        if not matches:
            return _failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                "Codex source credentials belong to another account.",
            )
    return _prepare_auth_bundle(
        account,
        bundle_path,
        source_blob,
        f"{CODEX_FILE_AUTH_CONFIG}\n".encode(),
        reference_time,
    )


def prepare_private_bundle_from_auth_bytes(
    account: Account,
    bundle_path: Path,
    source_auth: bytes | None,
    *,
    reference_time: datetime,
) -> PreparedCodexAuthBundle | ProviderFailure:
    """Purely prepare a bundle from coordinator-owned source bytes."""
    try:
        source_blob = (
            {} if source_auth is None else decode_json_object(source_auth)
        )
    except InvalidPayloadError:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The saved Codex auth bundle is malformed; import it again.",
        )
    try:
        matches = not source_blob or auth_blob_matches_account(
            source_blob,
            account,
        )
    except ProviderBoundaryError as error:
        return error.failure
    if not matches:
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "Codex source credentials belong to another account.",
        )
    return _prepare_auth_bundle(
        account,
        bundle_path,
        source_blob,
        f"{CODEX_FILE_AUTH_CONFIG}\n".encode(),
        reference_time,
    )


def prepare_export_bundle(
    account: Account,
    target_home: Path,
    *,
    source_homes: tuple[Path, ...],
    existing_config: bytes | None,
    reference_time: datetime,
) -> PreparedCodexAuthBundle | ProviderFailure:
    """Purely prepare an isolated export without writing any home."""
    target_result = _validated_export_target(target_home, source_homes)
    if isinstance(target_result, ProviderFailure):
        return target_result
    source_blob = _matching_source_blob(account, source_homes)
    if isinstance(source_blob, ProviderFailure):
        return source_blob
    config = prepare_file_auth_config(existing_config)
    if isinstance(config, ProviderFailure):
        return config
    return _prepare_auth_bundle(
        account,
        target_result,
        source_blob or {},
        config,
        reference_time,
    )


def _validated_export_target(
    target_home: Path,
    source_homes: tuple[Path, ...],
) -> Path | ProviderFailure:
    target = target_home.expanduser()
    if target.name == CODEX_AUTH_FILE:
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "A Codex export target must be a dedicated home directory.",
        )
    try:
        active_auth = codex_auth_path(default_codex_home()).resolve()
        target_auth = codex_auth_path(target).resolve()
        source_auth_paths = tuple(
            codex_auth_path(source_home).resolve()
            for source_home in source_homes
        )
    except OSError:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "The requested Codex export path could not be resolved.",
        )
    if target_auth == active_auth or target_auth in source_auth_paths:
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "Refusing to export over an active or source Codex auth home.",
        )
    return target


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
        return _failure(
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
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "The managed Codex home is not configured for file auth.",
        )
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, tomllib.TOMLDecodeError:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The managed Codex config is malformed.",
        )
    if document.get("cli_auth_credentials_store") != "file":
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "The managed Codex home is not configured for file auth.",
        )
    return None


def validate_auth_bundle_owner(
    payload: bytes,
    expected_account_id: str | None,
) -> ProviderFailure | None:
    """Validate one existing auth bundle against its saved owner."""
    if expected_account_id is None:
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The existing Codex auth bundle has no provable saved owner.",
        )
    try:
        blob = decode_json_object(payload)
        observed_id = auth_blob_account_id(blob)
    except InvalidPayloadError, ProviderBoundaryError:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The existing Codex auth bundle is malformed.",
        )
    if observed_id != expected_account_id:
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The existing Codex auth bundle belongs to another account.",
        )
    return None


def validate_auth_bundle_matches_account(
    payload: bytes,
    account: Account,
) -> ProviderFailure | None:
    """Prove bundle ownership by saved identity or exact access token."""
    try:
        blob = decode_json_object(payload)
        matches = auth_blob_matches_account(blob, account)
    except InvalidPayloadError, ProviderBoundaryError:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "The existing Codex auth bundle is malformed.",
        )
    if not matches:
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The existing Codex auth bundle belongs to another account.",
        )
    return None


def _prepare_auth_bundle(
    account: Account,
    bundle_path: Path,
    source_blob: JsonObject,
    config: bytes,
    reference_time: datetime,
) -> PreparedCodexAuthBundle | ProviderFailure:
    """Encode one complete auth bundle and its resulting credentials."""
    try:
        prepared = _prepared_auth_blob(
            account,
            source_blob,
            reference_time,
        )
    except ProviderBoundaryError as error:
        return error.failure
    if prepared is None:
        return _failure(
            ProviderFailureKind.INCOMPLETE,
            "The saved Codex account lacks complete export credentials.",
        )
    blob, id_token, account_id = prepared
    credentials = _prepared_bundle_credentials(
        account,
        bundle_path,
        blob,
        id_token,
        account_id,
    )
    return PreparedCodexAuthBundle(
        bundle_path.expanduser(),
        (
            (CODEX_CONFIG_FILE, config),
            (CODEX_AUTH_FILE, json.dumps(blob, indent=2).encode()),
        ),
        credentials,
    )


def auth_blob_matches_account(blob: JsonObject, account: Account) -> bool:
    """Return whether a validated Codex auth blob belongs to ``account``."""
    if account.provider_account_id is not None:
        return auth_blob_account_id(blob) == account.provider_account_id
    return auth_blob_access_token(blob) == account.access_token


def _matching_source_blob(
    account: Account,
    source_homes: tuple[Path, ...],
) -> JsonObject | ProviderFailure | None:
    for source_home in source_homes:
        source = read_auth_blob(source_home)
        if isinstance(source, ProviderFailure):
            if source.kind is ProviderFailureKind.MISSING:
                continue
            return source
        try:
            if auth_blob_matches_account(source, account):
                return source
        except ProviderBoundaryError as error:
            return error.failure
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "Codex source credentials belong to another account.",
        )
    return None


def require_codex_credentials(account: Account) -> CodexCredentials:
    """Return Codex credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(credentials, CodexCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Codex account.")


def _prepared_auth_blob(
    account: Account,
    existing: JsonObject,
    reference_time: datetime,
) -> tuple[JsonObject, str, str] | None:
    if (
        account.provider_id is not ProviderId.CODEX
        or not account.refresh_token
    ):
        return None
    existing_tokens = _auth_tokens(existing)
    id_token = _auth_id_token(account, existing_tokens)
    account_id = _auth_account_id(account)
    if not id_token or not account_id:
        return None
    blob = dict(existing)
    blob["auth_mode"] = _auth_mode(existing)
    blob["last_refresh"] = _auth_last_refresh(
        account,
        existing,
        reference_time,
    )
    blob["tokens"] = _updated_auth_tokens(
        existing_tokens,
        account,
        id_token,
        account_id,
    )
    return blob, id_token, account_id


def _prepared_bundle_credentials(
    account: Account,
    codex_home: Path,
    blob: JsonObject,
    id_token: str,
    account_id: str,
) -> CodexCredentials:
    """Return credentials that reference one prepared auth bundle."""
    credentials = require_codex_credentials(account)
    last_refresh = blob["last_refresh"]
    if not isinstance(last_refresh, str):
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex auth refresh metadata is malformed.",
            )
        )
    return replace(
        credentials,
        auth_home=str(codex_home.expanduser()),
        id_token=id_token,
        auth_last_refresh=last_refresh,
        account_id=account_id,
    )


def _auth_tokens(existing: JsonObject) -> JsonObject:
    tokens = existing.get("tokens")
    if tokens is None:
        return {}
    if isinstance(tokens, dict):
        return dict(tokens)
    raise ProviderBoundaryError(
        _failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json token metadata is malformed.",
        )
    )


def _auth_id_token(
    account: Account,
    existing_tokens: JsonObject,
) -> str | None:
    if account.codex_id_token:
        return account.codex_id_token
    existing_id = existing_tokens.get("id_token")
    if existing_id is None:
        return None
    if isinstance(existing_id, str) and existing_id:
        return existing_id
    raise ProviderBoundaryError(
        _failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json id-token metadata is malformed.",
        )
    )


def _auth_account_id(account: Account) -> str | None:
    if account.provider_account_id:
        return account.provider_account_id
    return account_id_from_token(account.access_token)


def _auth_mode(existing: JsonObject) -> str:
    auth_mode = existing.get("auth_mode")
    if auth_mode is None:
        return "chatgpt"
    if isinstance(auth_mode, str) and auth_mode:
        return auth_mode
    raise ProviderBoundaryError(
        _failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json auth-mode metadata is malformed.",
        )
    )


def _auth_last_refresh(
    account: Account,
    existing: JsonObject,
    reference_time: datetime,
) -> str:
    if account.codex_last_refresh:
        return account.codex_last_refresh
    existing_last_refresh = existing.get("last_refresh")
    if existing_last_refresh is None:
        return codex_timestamp(reference_time)
    if isinstance(existing_last_refresh, str) and existing_last_refresh:
        return existing_last_refresh
    raise ProviderBoundaryError(
        _failure(
            ProviderFailureKind.MALFORMED,
            "Codex auth.json refresh metadata is malformed.",
        )
    )


def _updated_auth_tokens(
    existing_tokens: JsonObject,
    account: Account,
    id_token: str,
    account_id: str,
) -> JsonObject:
    tokens = dict(existing_tokens)
    tokens.update(
        {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": id_token,
            "account_id": account_id,
        }
    )
    return tokens


def codex_timestamp(value: datetime) -> str:
    """Return a Codex auth.json-style aware UTC timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Codex auth timestamp must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
