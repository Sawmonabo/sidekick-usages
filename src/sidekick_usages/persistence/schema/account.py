"""Strict schema-version-three no-secret account index codec."""

import json
from datetime import datetime

from sidekick_usages.core.accounts.models import (
    AccountAuthority,
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    ClaudeStoredLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    InvalidSchemaError,
    MalformedJsonError,
)
from sidekick_usages.persistence.limits import (
    MAX_DOCUMENT_BYTES,
)
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.schema.validation import (
    bounded_text,
    canonical_account_id_text,
)
from sidekick_usages.persistence.state.fields import (
    require_boolean,
    require_exact_keys,
    require_list,
    require_object,
    require_optional_string,
    require_schema_version,
    require_string,
)
from sidekick_usages.persistence.state.validation import (
    validate_non_secret_state,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonObject,
    JsonValue,
    decode_json_value,
)

SCHEMA_VERSION = 3
_MAX_METADATA_BYTES = 4_096

_ACCOUNT_KEYS = frozenset(
    {
        "authority",
        "credential_health",
        "heartbeat_enabled",
        "heartbeat_targets",
        "heartbeat_window_resets",
        "label",
        "last_heartbeat_at",
        "last_heartbeat_error_code",
        "last_heartbeat_status",
        "last_refresh_at",
        "last_refresh_error_code",
        "last_refresh_status",
        "plan",
        "provider_id",
    }
)
_CLAUDE_AUTHORITY_KEYS = frozenset(
    {
        "provider_id",
        "setup_token",
        "subscription",
    }
)
_CLAUDE_MANAGED_KEYS = frozenset(
    {
        "access_expires_at",
        "action",
        "authority_id",
        "executable_version",
        "generation",
        "health",
        "kind",
        "provider_identity",
        "refresh_expires_at",
        "verified_at",
    }
)
_CLAUDE_STORED_KEYS = frozenset(
    {
        "access_expires_at",
        "authority_id",
        "health",
        "kind",
        "observed_at",
        "provider_identity",
        "refresh_expires_at",
    }
)
_CODEX_AUTHORITY_KEYS = frozenset({"provider_id", "subscription"})
_CODEX_MANAGED_KEYS = frozenset(
    {
        "authority_id",
        "executable_version",
        "generation",
        "health",
        "kind",
        "provider_identity",
        "verified_at",
    }
)
_CODEX_STORED_KEYS = frozenset(
    {
        "authority_id",
        "expires_at",
        "generation",
        "health",
        "kind",
        "observed_at",
        "provider_identity",
    }
)
_ENVELOPE_KEYS = frozenset({"accounts", "schema_version"})
_SETUP_TOKEN_KEYS = frozenset(
    {
        "authority_id",
        "expires_at",
        "health",
        "observed_at",
    }
)


def _required_bounded_text(value: JsonValue) -> str:
    return bounded_text(require_string(value), _MAX_METADATA_BYTES)


def _optional_bounded_text(value: JsonValue) -> str | None:
    text = require_optional_string(value)
    return None if text is None else bounded_text(text, _MAX_METADATA_BYTES)


def _authority_id(value: JsonValue) -> AuthorityId:
    return AuthorityId(canonical_account_id_text(require_string(value)))


def _time(value: str | None) -> datetime | None:
    """Decode one optional canonical timestamp."""
    return None if value is None else parse_canonical_timestamp(value)


def _identity(value: str | None) -> ProviderIdentity | None:
    """Decode one optional provider identity."""
    return None if value is None else ProviderIdentity(value)


def _setup_authority(value: JsonValue) -> ClaudeSetupTokenAuthority | None:
    if value is None:
        return None
    record = require_object(value)
    require_exact_keys(record, _SETUP_TOKEN_KEYS)
    return ClaudeSetupTokenAuthority(
        authority_id=_authority_id(record["authority_id"]),
        expires_at=_time(require_optional_string(record["expires_at"])),
        health=CredentialHealth(require_string(record["health"])),
        observed_at=_time(require_optional_string(record["observed_at"])),
    )


def _claude_stored_authority(
    record: JsonObject,
) -> ClaudeStoredLoginAuthority:
    require_exact_keys(record, _CLAUDE_STORED_KEYS)
    if require_string(record["kind"]) != "stored":
        raise InvalidSchemaError
    return ClaudeStoredLoginAuthority(
        authority_id=_authority_id(record["authority_id"]),
        provider_identity=_identity(
            _optional_bounded_text(record["provider_identity"])
        ),
        access_expires_at=_time(
            require_optional_string(record["access_expires_at"])
        ),
        refresh_expires_at=_time(
            require_optional_string(record["refresh_expires_at"])
        ),
        health=CredentialHealth(require_string(record["health"])),
        observed_at=_time(require_optional_string(record["observed_at"])),
    )


def _claude_managed_authority(
    record: JsonObject,
) -> ClaudeManagedLoginAuthority:
    require_exact_keys(record, _CLAUDE_MANAGED_KEYS)
    if require_string(record["kind"]) != "managed":
        raise InvalidSchemaError
    return ClaudeManagedLoginAuthority(
        authority_id=_authority_id(record["authority_id"]),
        provider_identity=ProviderIdentity(
            _required_bounded_text(record["provider_identity"])
        ),
        generation=AuthorityGeneration(
            _required_bounded_text(record["generation"])
        ),
        access_expires_at=parse_canonical_timestamp(
            require_string(record["access_expires_at"])
        ),
        refresh_expires_at=_time(
            require_optional_string(record["refresh_expires_at"])
        ),
        verified_at=parse_canonical_timestamp(
            require_string(record["verified_at"])
        ),
        executable_version=_required_bounded_text(
            record["executable_version"]
        ),
        health=CredentialHealth(require_string(record["health"])),
        action=CredentialAction(require_string(record["action"])),
    )


def _claude_subscription(
    value: JsonValue,
) -> ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority | None:
    if value is None:
        return None
    record = require_object(value)
    kind = require_string(record.get("kind"))
    if kind == "stored":
        return _claude_stored_authority(record)
    if kind == "managed":
        return _claude_managed_authority(record)
    raise InvalidSchemaError


def _claude_authority(value: JsonValue) -> AccountAuthority:
    """Convert strict Claude authority metadata to core types."""
    record = require_object(value)
    require_exact_keys(record, _CLAUDE_AUTHORITY_KEYS)
    if require_string(record["provider_id"]) != ProviderId.CLAUDE.value:
        raise InvalidSchemaError
    return ClaudeAccountAuthority(
        setup_token=_setup_authority(record["setup_token"]),
        subscription=_claude_subscription(record["subscription"]),
    )


def _codex_stored_authority(record: JsonObject) -> CodexStoredAuthority:
    require_exact_keys(record, _CODEX_STORED_KEYS)
    if require_string(record["kind"]) != "stored":
        raise InvalidSchemaError
    generation = _optional_bounded_text(record["generation"])
    return CodexStoredAuthority(
        authority_id=_authority_id(record["authority_id"]),
        provider_identity=_identity(
            _optional_bounded_text(record["provider_identity"])
        ),
        expires_at=_time(require_optional_string(record["expires_at"])),
        generation=(
            None if generation is None else AuthorityGeneration(generation)
        ),
        health=CredentialHealth(require_string(record["health"])),
        observed_at=_time(require_optional_string(record["observed_at"])),
    )


def _codex_managed_authority(record: JsonObject) -> CodexManagedAuthority:
    require_exact_keys(record, _CODEX_MANAGED_KEYS)
    if require_string(record["kind"]) != "managed":
        raise InvalidSchemaError
    return CodexManagedAuthority(
        authority_id=_authority_id(record["authority_id"]),
        provider_identity=ProviderIdentity(
            _required_bounded_text(record["provider_identity"])
        ),
        generation=AuthorityGeneration(
            _required_bounded_text(record["generation"])
        ),
        verified_at=parse_canonical_timestamp(
            require_string(record["verified_at"])
        ),
        executable_version=_required_bounded_text(
            record["executable_version"]
        ),
        health=CredentialHealth(require_string(record["health"])),
    )


def _codex_authority(value: JsonValue) -> AccountAuthority:
    """Convert strict Codex authority metadata to core types."""
    record = require_object(value)
    require_exact_keys(record, _CODEX_AUTHORITY_KEYS)
    if require_string(record["provider_id"]) != ProviderId.CODEX.value:
        raise InvalidSchemaError
    subscription = require_object(record["subscription"])
    kind = require_string(subscription.get("kind"))
    if kind == "stored":
        authority = _codex_stored_authority(subscription)
    elif kind == "managed":
        authority = _codex_managed_authority(subscription)
    else:
        raise InvalidSchemaError
    return CodexAccountAuthority(subscription=authority)


def _heartbeat_resets(
    value: JsonValue,
) -> tuple[tuple[str, datetime], ...] | None:
    if value is None:
        return None
    resets = require_object(value)
    return tuple(
        (
            bounded_text(target, _MAX_METADATA_BYTES),
            parse_canonical_timestamp(require_string(reset_at)),
        )
        for target, reset_at in resets.items()
    )


def _heartbeat_targets(value: JsonValue) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(
        _required_bounded_text(target) for target in require_list(value)
    )


def _decode_root(payload: bytes) -> JsonObject:
    """Decode one bounded strict JSON object with safe errors."""
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    try:
        decoded = decode_json_value(payload)
    except JsonDecodeError as error:
        if error.code is JsonDecodeCode.DUPLICATE_KEY:
            raise DuplicateKeyError from None
        raise MalformedJsonError from None
    if not isinstance(decoded, dict):
        raise InvalidSchemaError
    validate_non_secret_state(decoded)
    return decoded


def _account(account_id: str, value: JsonValue) -> SavedAccount:
    """Convert one strict saved-account record."""
    record = require_object(value)
    require_exact_keys(record, _ACCOUNT_KEYS)
    provider_id = ProviderId(require_string(record["provider_id"]))
    authority = (
        _claude_authority(record["authority"])
        if provider_id is ProviderId.CLAUDE
        else _codex_authority(record["authority"])
    )
    refresh_status = require_optional_string(record["last_refresh_status"])
    heartbeat_status = require_optional_string(record["last_heartbeat_status"])
    return SavedAccount(
        account_id=SidekickAccountId(account_id),
        label=AccountLabel(_required_bounded_text(record["label"])),
        provider_id=provider_id,
        plan=_required_bounded_text(record["plan"]),
        authority=authority,
        credential_health=CredentialHealth(
            require_string(record["credential_health"])
        ),
        last_refresh_at=_time(
            require_optional_string(record["last_refresh_at"])
        ),
        last_refresh_status=(
            None if refresh_status is None else RefreshStatus(refresh_status)
        ),
        last_refresh_error_code=_optional_bounded_text(
            record["last_refresh_error_code"]
        ),
        heartbeat_enabled=require_boolean(record["heartbeat_enabled"]),
        heartbeat_window_resets=_heartbeat_resets(
            record["heartbeat_window_resets"]
        ),
        heartbeat_targets=_heartbeat_targets(record["heartbeat_targets"]),
        last_heartbeat_at=_time(
            require_optional_string(record["last_heartbeat_at"])
        ),
        last_heartbeat_status=(
            None
            if heartbeat_status is None
            else HeartbeatStatus(heartbeat_status)
        ),
        last_heartbeat_error_code=_optional_bounded_text(
            record["last_heartbeat_error_code"]
        ),
    )


def decode_version_three(payload: bytes) -> VersionThreeDocument:
    """Decode one strict no-secret schema-version-three account index."""
    root = _decode_root(payload)
    require_exact_keys(root, _ENVELOPE_KEYS)
    require_schema_version(root["schema_version"], SCHEMA_VERSION)
    accounts = require_object(root["accounts"])
    try:
        document = VersionThreeDocument(
            tuple(
                _account(canonical_account_id_text(account_id), account)
                for account_id, account in accounts.items()
            )
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    return document


def _timestamp(value: datetime | None) -> JsonValue:
    """Encode one optional aware timestamp."""
    return None if value is None else canonical_timestamp(value)


def _setup_object(
    authority: ClaudeSetupTokenAuthority | None,
) -> JsonObject | None:
    """Encode optional setup-token reference metadata."""
    if authority is None:
        return None
    return {
        "authority_id": str(authority.authority_id),
        "expires_at": _timestamp(authority.expires_at),
        "health": authority.health.value,
        "observed_at": _timestamp(authority.observed_at),
    }


def _subscription_object(
    authority: (
        ClaudeStoredLoginAuthority
        | ClaudeManagedLoginAuthority
        | CodexStoredAuthority
        | CodexManagedAuthority
    ),
) -> JsonObject:
    """Encode one provider subscription authority."""
    if isinstance(authority, ClaudeStoredLoginAuthority):
        return {
            "kind": "stored",
            "authority_id": str(authority.authority_id),
            "provider_identity": (
                str(authority.provider_identity)
                if authority.provider_identity is not None
                else None
            ),
            "access_expires_at": _timestamp(authority.access_expires_at),
            "refresh_expires_at": _timestamp(authority.refresh_expires_at),
            "health": authority.health.value,
            "observed_at": _timestamp(authority.observed_at),
        }
    if isinstance(authority, CodexStoredAuthority):
        return {
            "kind": "stored",
            "authority_id": str(authority.authority_id),
            "provider_identity": (
                str(authority.provider_identity)
                if authority.provider_identity is not None
                else None
            ),
            "expires_at": _timestamp(authority.expires_at),
            "generation": (
                str(authority.generation)
                if authority.generation is not None
                else None
            ),
            "health": authority.health.value,
            "observed_at": _timestamp(authority.observed_at),
        }
    if isinstance(authority, ClaudeManagedLoginAuthority):
        return {
            "kind": "managed",
            "authority_id": str(authority.authority_id),
            "provider_identity": str(authority.provider_identity),
            "generation": str(authority.generation),
            "access_expires_at": canonical_timestamp(
                authority.access_expires_at
            ),
            "refresh_expires_at": _timestamp(authority.refresh_expires_at),
            "verified_at": canonical_timestamp(authority.verified_at),
            "executable_version": authority.executable_version,
            "health": authority.health.value,
            "action": authority.action.value,
        }
    return {
        "kind": "managed",
        "authority_id": str(authority.authority_id),
        "provider_identity": str(authority.provider_identity),
        "generation": str(authority.generation),
        "verified_at": canonical_timestamp(authority.verified_at),
        "executable_version": authority.executable_version,
        "health": authority.health.value,
    }


def _authority_object(authority: AccountAuthority) -> JsonObject:
    """Encode one provider-discriminated account authority."""
    if isinstance(authority, ClaudeAccountAuthority):
        return {
            "provider_id": "claude",
            "setup_token": _setup_object(authority.setup_token),
            "subscription": (
                _subscription_object(authority.subscription)
                if authority.subscription is not None
                else None
            ),
        }
    return {
        "provider_id": "codex",
        "subscription": _subscription_object(authority.subscription),
    }


def _account_object(account: SavedAccount) -> JsonObject:
    """Encode one canonical saved-account record."""
    resets = account.heartbeat_window_resets
    return {
        "label": str(account.label),
        "provider_id": account.provider_id.value,
        "plan": account.plan,
        "authority": _authority_object(account.authority),
        "credential_health": account.credential_health.value,
        "last_refresh_at": _timestamp(account.last_refresh_at),
        "last_refresh_status": (
            account.last_refresh_status.value
            if account.last_refresh_status is not None
            else None
        ),
        "last_refresh_error_code": account.last_refresh_error_code,
        "heartbeat_enabled": account.heartbeat_enabled,
        "heartbeat_window_resets": (
            {
                target: canonical_timestamp(reset_at)
                for target, reset_at in resets
            }
            if resets is not None
            else None
        ),
        "heartbeat_targets": (
            list(account.heartbeat_targets)
            if account.heartbeat_targets is not None
            else None
        ),
        "last_heartbeat_at": _timestamp(account.last_heartbeat_at),
        "last_heartbeat_status": (
            account.last_heartbeat_status.value
            if account.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error_code": account.last_heartbeat_error_code,
    }


def encode_version_three(document: VersionThreeDocument) -> bytes:
    """Encode deterministic canonical schema-version-three bytes."""
    accounts: JsonObject = {
        str(account.account_id): _account_object(account)
        for account in document.accounts
    }
    root: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "accounts": accounts,
    }
    validate_non_secret_state(root)
    try:
        payload = (
            json.dumps(
                root,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        raise InvalidSchemaError from None
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    if decode_version_three(payload) != document:
        raise InvalidSchemaError
    return payload
