"""Strict schema-version-three no-secret account index codec."""

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from sidekick_usages.core.accounts.models import (
    AccountAuthority,
    ClaudeAccountAuthority,
    ClaudeLegacyLoginAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    CodexAccountAuthority,
    CodexLegacyAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
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
from sidekick_usages.persistence.state_validation import (
    validate_non_secret_state,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonObject,
    JsonValue,
    decode_json_value,
)

SCHEMA_VERSION = 3
_MAX_METADATA_BYTES = 4_096
_MODEL_CONFIG = ConfigDict(
    strict=True,
    extra="forbid",
    frozen=True,
)

type _Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
type _Timestamp = Annotated[str, AfterValidator(_canonical_time)]
type _BoundedText = Annotated[str, AfterValidator(_bounded_text)]
type _Health = Literal[
    "healthy",
    "refresh_due",
    "login_required",
    "migration_required",
    "unreadable",
    "malformed",
    "unsupported",
    "reconciliation_required",
    "unknown",
]
type _ClaudeSubscriptionModel = Annotated[
    _ClaudeLegacyModel | _ClaudeManagedModel,
    Field(discriminator="kind"),
]
type _CodexSubscriptionModel = Annotated[
    _CodexLegacyModel | _CodexManagedModel,
    Field(discriminator="kind"),
]
type _AccountModel = Annotated[
    _ClaudeAccountModel | _CodexAccountModel,
    Field(discriminator="provider_id"),
]


def _canonical_uuid(value: str) -> str:
    """Validate a canonical Sidekick UUID while preserving its text."""
    SidekickAccountId(value)
    return value


def _canonical_time(value: str) -> str:
    """Validate one canonical persisted UTC timestamp."""
    parse_canonical_timestamp(value)
    return value


def _bounded_text(value: str) -> str:
    """Require one bounded nonempty UTF-8 metadata value."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Metadata must be valid UTF-8.") from None
    if not encoded or len(encoded) > _MAX_METADATA_BYTES:
        raise ValueError("Metadata must be nonempty and bounded.")
    return value


class _ClaudeSetupTokenModel(BaseModel):
    """Validated setup-token reference metadata."""

    model_config = _MODEL_CONFIG

    authority_id: _Uuid
    expires_at: _Timestamp | None
    health: _Health
    observed_at: _Timestamp | None


class _ClaudeLegacyModel(BaseModel):
    """Validated pre-managed Claude subscription metadata."""

    model_config = _MODEL_CONFIG

    kind: Literal["legacy"]
    authority_id: _Uuid
    provider_identity: _BoundedText | None
    access_expires_at: _Timestamp | None
    refresh_expires_at: _Timestamp | None
    health: _Health
    observed_at: _Timestamp | None


class _ClaudeManagedModel(BaseModel):
    """Validated managed Claude subscription metadata."""

    model_config = _MODEL_CONFIG

    kind: Literal["managed"]
    authority_id: _Uuid
    provider_identity: _BoundedText
    generation: _BoundedText
    verified_at: _Timestamp
    executable_version: _BoundedText
    health: _Health


class _ClaudeAuthorityModel(BaseModel):
    """Validated Claude authority envelope."""

    model_config = _MODEL_CONFIG

    provider_id: Literal["claude"]
    setup_token: _ClaudeSetupTokenModel | None
    subscription: _ClaudeSubscriptionModel | None


class _CodexLegacyModel(BaseModel):
    """Validated pre-managed Codex subscription metadata."""

    model_config = _MODEL_CONFIG

    kind: Literal["legacy"]
    authority_id: _Uuid
    provider_identity: _BoundedText | None
    expires_at: _Timestamp | None
    generation: _BoundedText | None
    health: _Health
    observed_at: _Timestamp | None


class _CodexManagedModel(BaseModel):
    """Validated managed Codex subscription metadata."""

    model_config = _MODEL_CONFIG

    kind: Literal["managed"]
    authority_id: _Uuid
    provider_identity: _BoundedText
    generation: _BoundedText
    verified_at: _Timestamp
    executable_version: _BoundedText
    health: _Health


class _CodexAuthorityModel(BaseModel):
    """Validated Codex authority envelope."""

    model_config = _MODEL_CONFIG

    provider_id: Literal["codex"]
    subscription: _CodexSubscriptionModel


class _AccountStateModel(BaseModel):
    """Strict provider-neutral saved-account state."""

    model_config = _MODEL_CONFIG

    label: _BoundedText
    plan: _BoundedText
    credential_health: _Health
    last_refresh_at: _Timestamp | None
    last_refresh_status: Literal["ok", "skipped", "failed"] | None
    last_refresh_error_code: _BoundedText | None
    heartbeat_enabled: bool
    heartbeat_5h_reset_at: _Timestamp | None
    heartbeat_window_resets: dict[_BoundedText, _Timestamp] | None
    heartbeat_targets: list[_BoundedText] | None
    last_heartbeat_at: _Timestamp | None
    last_heartbeat_status: (
        Literal[
            "warmed",
            "active",
            "disabled",
            "unsupported",
            "failed",
            "enabled",
        ]
        | None
    )
    last_heartbeat_error_code: _BoundedText | None


class _ClaudeAccountModel(_AccountStateModel):
    """Strict Claude saved-account record."""

    provider_id: Literal["claude"]
    authority: _ClaudeAuthorityModel


class _CodexAccountModel(_AccountStateModel):
    """Strict Codex saved-account record."""

    provider_id: Literal["codex"]
    authority: _CodexAuthorityModel


class _EnvelopeModel(BaseModel):
    """Strict schema-version-three document envelope."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[3]
    accounts: dict[_Uuid, _AccountModel]


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


def _time(value: str | None) -> datetime | None:
    """Decode one optional canonical timestamp."""
    return None if value is None else parse_canonical_timestamp(value)


def _identity(value: str | None) -> ProviderIdentity | None:
    """Decode one optional provider identity."""
    return None if value is None else ProviderIdentity(value)


def _claude_authority(model: _ClaudeAuthorityModel) -> AccountAuthority:
    """Convert validated Claude authority metadata to core types."""
    setup = model.setup_token
    setup_authority = (
        None
        if setup is None
        else ClaudeSetupTokenAuthority(
            authority_id=AuthorityId(setup.authority_id),
            expires_at=_time(setup.expires_at),
            health=CredentialHealth(setup.health),
            observed_at=_time(setup.observed_at),
        )
    )
    subscription = model.subscription
    if isinstance(subscription, _ClaudeLegacyModel):
        subscription_authority = ClaudeLegacyLoginAuthority(
            authority_id=AuthorityId(subscription.authority_id),
            provider_identity=_identity(subscription.provider_identity),
            access_expires_at=_time(subscription.access_expires_at),
            refresh_expires_at=_time(subscription.refresh_expires_at),
            health=CredentialHealth(subscription.health),
            observed_at=_time(subscription.observed_at),
        )
    elif isinstance(subscription, _ClaudeManagedModel):
        subscription_authority = ClaudeManagedLoginAuthority(
            authority_id=AuthorityId(subscription.authority_id),
            provider_identity=ProviderIdentity(subscription.provider_identity),
            generation=AuthorityGeneration(subscription.generation),
            verified_at=parse_canonical_timestamp(subscription.verified_at),
            executable_version=subscription.executable_version,
            health=CredentialHealth(subscription.health),
        )
    else:
        subscription_authority = None
    return ClaudeAccountAuthority(
        setup_token=setup_authority,
        subscription=subscription_authority,
    )


def _codex_authority(model: _CodexAuthorityModel) -> AccountAuthority:
    """Convert validated Codex authority metadata to core types."""
    subscription = model.subscription
    if isinstance(subscription, _CodexLegacyModel):
        authority = CodexLegacyAuthority(
            authority_id=AuthorityId(subscription.authority_id),
            provider_identity=_identity(subscription.provider_identity),
            expires_at=_time(subscription.expires_at),
            generation=(
                AuthorityGeneration(subscription.generation)
                if subscription.generation is not None
                else None
            ),
            health=CredentialHealth(subscription.health),
            observed_at=_time(subscription.observed_at),
        )
    else:
        authority = CodexManagedAuthority(
            authority_id=AuthorityId(subscription.authority_id),
            provider_identity=ProviderIdentity(subscription.provider_identity),
            generation=AuthorityGeneration(subscription.generation),
            verified_at=parse_canonical_timestamp(subscription.verified_at),
            executable_version=subscription.executable_version,
            health=CredentialHealth(subscription.health),
        )
    return CodexAccountAuthority(subscription=authority)


def _account(
    account_id: str,
    model: _ClaudeAccountModel | _CodexAccountModel,
) -> SavedAccount:
    """Convert one validated saved-account model."""
    authority = (
        _claude_authority(model.authority)
        if isinstance(model, _ClaudeAccountModel)
        else _codex_authority(model.authority)
    )
    resets = model.heartbeat_window_resets
    return SavedAccount(
        account_id=SidekickAccountId(account_id),
        label=AccountLabel(model.label),
        provider_id=ProviderId(model.provider_id),
        plan=model.plan,
        authority=authority,
        credential_health=CredentialHealth(model.credential_health),
        last_refresh_at=_time(model.last_refresh_at),
        last_refresh_status=(
            RefreshStatus(model.last_refresh_status)
            if model.last_refresh_status is not None
            else None
        ),
        last_refresh_error_code=model.last_refresh_error_code,
        heartbeat_enabled=model.heartbeat_enabled,
        heartbeat_5h_reset_at=_time(model.heartbeat_5h_reset_at),
        heartbeat_window_resets=(
            tuple(
                (target, parse_canonical_timestamp(reset_at))
                for target, reset_at in resets.items()
            )
            if resets is not None
            else None
        ),
        heartbeat_targets=(
            tuple(model.heartbeat_targets)
            if model.heartbeat_targets is not None
            else None
        ),
        last_heartbeat_at=_time(model.last_heartbeat_at),
        last_heartbeat_status=(
            HeartbeatStatus(model.last_heartbeat_status)
            if model.last_heartbeat_status is not None
            else None
        ),
        last_heartbeat_error_code=model.last_heartbeat_error_code,
    )


def decode_version_three(payload: bytes) -> VersionThreeDocument:
    """Decode one strict no-secret schema-version-three account index."""
    root = _decode_root(payload)
    try:
        envelope = _EnvelopeModel.model_validate(root, strict=True)
        document = VersionThreeDocument(
            tuple(
                _account(account_id, account)
                for account_id, account in envelope.accounts.items()
            )
        )
    except ValidationError, TypeError, ValueError:
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
        ClaudeLegacyLoginAuthority
        | ClaudeManagedLoginAuthority
        | CodexLegacyAuthority
        | CodexManagedAuthority
    ),
) -> JsonObject:
    """Encode one provider subscription authority."""
    if isinstance(authority, ClaudeLegacyLoginAuthority):
        return {
            "kind": "legacy",
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
    if isinstance(authority, CodexLegacyAuthority):
        return {
            "kind": "legacy",
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
        "heartbeat_5h_reset_at": _timestamp(account.heartbeat_5h_reset_at),
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


def has_managed_authority(document: VersionThreeDocument) -> bool:
    """Return whether rollback must reject the complete account index."""
    return any(account.has_managed_authority for account in document.accounts)
