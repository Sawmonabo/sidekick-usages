"""Codex auth discovery, identity, login, and pure bundle preparation."""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import InvalidPayloadError, UsageError
from sidekick_usages.providers.base import (
    CredentialDetection,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.schemas import (
    account_id_from_token,
    auth_blob_access_token,
    auth_blob_account_id,
    parse_auth_credentials,
)
from sidekick_usages.serialization import JsonObject, decode_json_object

CODEX_HOME_ENV = "CODEX_HOME"
CODEX_AUTH_FILE = "auth.json"
CODEX_CONFIG_FILE = "config.toml"
CODEX_FILE_AUTH_CONFIG = 'cli_auth_credentials_store = "file"'
_MAX_AUTH_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LoginSuccess:
    """Codex login completed against the requested source home."""

    source_home: Path | None


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


def default_codex_home() -> Path:
    """Return the Codex home used by default credential detection."""
    configured = os.environ.get(CODEX_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


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
        with path.open("rb") as stream:
            payload = stream.read(_MAX_AUTH_BYTES + 1)
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
    if len(payload) > _MAX_AUTH_BYTES:
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


def run_login(
    source_home: Path | None,
    *,
    device_auth: bool,
) -> LoginSuccess | ProviderFailure:
    """Run Codex login without terminal or presentation behavior."""
    normalized = source_home.expanduser() if source_home is not None else None
    try:
        command = ["codex", "login"]
        if device_auth:
            command.append("--device-auth")
        if normalized is None:
            subprocess.run(command, check=True)
        else:
            environment = os.environ.copy()
            environment[CODEX_HOME_ENV] = str(normalized)
            subprocess.run(command, check=True, env=environment)
    except FileNotFoundError:
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "Codex CLI was not found on PATH; install it and retry.",
        )
    except subprocess.CalledProcessError:
        return _failure(
            ProviderFailureKind.REJECTED,
            "Codex login did not complete successfully.",
        )
    except OSError, subprocess.SubprocessError:
        return _failure(
            ProviderFailureKind.UNREADABLE,
            "Codex login could not access its requested state home.",
        )
    return LoginSuccess(normalized)


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


__all__ = [
    "CODEX_AUTH_FILE",
    "CODEX_CONFIG_FILE",
    "CODEX_FILE_AUTH_CONFIG",
    "LoginSuccess",
    "PreparedCodexAuthBundle",
    "auth_blob_matches_account",
    "codex_auth_path",
    "codex_timestamp",
    "default_codex_home",
    "detect_auth_credentials",
    "prepare_export_bundle",
    "prepare_file_auth_config",
    "prepare_private_bundle",
    "prepare_private_bundle_from_auth_bytes",
    "read_auth_blob",
    "require_codex_credentials",
    "run_login",
    "validate_auth_bundle_matches_account",
    "validate_auth_bundle_owner",
]
