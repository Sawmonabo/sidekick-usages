"""Strict codec for protected provider credential material."""

import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.credential import (
    stored_credential_kind,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    JsonObject,
    decode_json_value,
)

CREDENTIAL_SCHEMA_VERSION = 1
MAX_CREDENTIAL_METADATA_BYTES = 4_096
MAX_CREDENTIAL_SECRET_BYTES = 1024 * 1024
MAX_CLAUDE_SCOPES = 128
MODEL_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


type TimestampValue = Annotated[str, AfterValidator(_timestamp)]
type SecretValue = Annotated[str, AfterValidator(_secret)]
type MetadataValue = Annotated[str, AfterValidator(_metadata)]
type CredentialModel = Annotated[
    ClaudeSetupCredentialModel
    | ClaudeSubscriptionCredentialModel
    | CodexSubscriptionCredentialModel,
    Field(discriminator="credential_kind"),
]


class CredentialDecodeError(ValueError):
    """Protected credential bytes violate the strict current schema."""


def _timestamp(value: str) -> str:
    parse_canonical_timestamp(value)
    return value


def _secret(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Credential material must be valid UTF-8.") from None
    if not encoded or len(encoded) > MAX_CREDENTIAL_SECRET_BYTES:
        raise ValueError("Credential material must be nonempty and bounded.")
    return value


def _metadata(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Credential metadata must be valid UTF-8.") from None
    if not encoded or len(encoded) > MAX_CREDENTIAL_METADATA_BYTES:
        raise ValueError("Credential metadata must be nonempty and bounded.")
    return value


class ClaudeIdentityModel(BaseModel):
    """Strict complete Claude provider identity."""

    model_config = MODEL_CONFIG

    account_id: MetadataValue
    organization_id: MetadataValue


class ClaudeSetupCredentialModel(BaseModel):
    """Strict protected Claude setup-token credentials."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1]
    provider_id: Literal["claude"]
    credential_kind: Literal["claude_setup"]
    access_token: SecretValue


class ClaudeSubscriptionCredentialModel(BaseModel):
    """Strict protected Claude subscription credentials."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1]
    provider_id: Literal["claude"]
    credential_kind: Literal["claude_login"]
    access_token: SecretValue
    refresh_token: SecretValue
    access_expires_at: TimestampValue
    refresh_expires_at: TimestampValue | None
    scopes: list[MetadataValue] = Field(
        min_length=1,
        max_length=MAX_CLAUDE_SCOPES,
    )
    identity: ClaudeIdentityModel | None


class CodexSubscriptionCredentialModel(BaseModel):
    """Strict protected Codex subscription credentials."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1]
    provider_id: Literal["codex"]
    credential_kind: Literal["codex_login"]
    access_token: SecretValue
    refresh_token: SecretValue | None
    expires_at: TimestampValue | None
    provider_account_id: MetadataValue | None
    auth_home: MetadataValue | None
    id_token: SecretValue | None
    auth_last_refresh: MetadataValue | None


def _credential_object(credentials: Credentials) -> JsonObject:
    common: JsonObject = {
        "schema_version": CREDENTIAL_SCHEMA_VERSION,
        "provider_id": credentials.provider_id.value,
        "credential_kind": stored_credential_kind(credentials).value,
        "access_token": credentials.access_token,
    }
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return common
    if isinstance(credentials, ClaudeLoginCredentials):
        identity = credentials.identity
        return {
            **common,
            "refresh_token": credentials.refresh_token,
            "access_expires_at": canonical_timestamp(
                credentials.access_expiry.at
            ),
            "refresh_expires_at": (
                canonical_timestamp(credentials.refresh_expiry.at)
                if isinstance(credentials.refresh_expiry, KnownExpiry)
                else None
            ),
            "scopes": list(credentials.scopes),
            "identity": (
                {
                    "account_id": identity.account_id,
                    "organization_id": identity.organization_id,
                }
                if identity is not None
                else None
            ),
        }
    return {
        **common,
        "refresh_token": credentials.refresh_token,
        "expires_at": (
            canonical_timestamp(credentials.expiry.at)
            if isinstance(credentials.expiry, KnownExpiry)
            else None
        ),
        "provider_account_id": credentials.account_id,
        "auth_home": credentials.auth_home,
        "id_token": credentials.id_token,
        "auth_last_refresh": credentials.auth_last_refresh,
    }


def encode_credentials(credentials: Credentials) -> bytes:
    """Encode one strict current protected credential record."""
    try:
        payload = json.dumps(
            _credential_object(credentials),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        raise CredentialDecodeError from None
    if (
        len(payload) > MAX_DOCUMENT_BYTES
        or decode_credentials(payload) != credentials
    ):
        raise CredentialDecodeError
    return payload


def _credentials(model: CredentialModel) -> Credentials:
    if isinstance(model, ClaudeSetupCredentialModel):
        return ClaudeSetupTokenCredentials(access_token=model.access_token)
    if isinstance(model, ClaudeSubscriptionCredentialModel):
        return ClaudeLoginCredentials(
            access_token=model.access_token,
            refresh_token=model.refresh_token,
            access_expiry=KnownExpiry(
                parse_canonical_timestamp(model.access_expires_at)
            ),
            refresh_expiry=(
                KnownExpiry(
                    parse_canonical_timestamp(model.refresh_expires_at)
                )
                if model.refresh_expires_at is not None
                else UnknownExpiry()
            ),
            scopes=tuple(model.scopes),
            identity=(
                ClaudeLoginIdentity(
                    account_id=model.identity.account_id,
                    organization_id=model.identity.organization_id,
                )
                if model.identity is not None
                else None
            ),
        )
    return CodexCredentials(
        access_token=model.access_token,
        refresh_token=model.refresh_token,
        expiry=(
            KnownExpiry(parse_canonical_timestamp(model.expires_at))
            if model.expires_at is not None
            else UnknownExpiry()
        ),
        account_id=model.provider_account_id,
        auth_home=model.auth_home,
        id_token=model.id_token,
        auth_last_refresh=model.auth_last_refresh,
    )


def decode_credentials(payload: bytes) -> Credentials:
    """Decode one strict current protected credential record."""
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise CredentialDecodeError
    try:
        value = decode_json_value(payload)
        model = TypeAdapter(CredentialModel).validate_python(
            value,
            strict=True,
        )
        return _credentials(model)
    except JsonDecodeError, ValidationError, TypeError, ValueError:
        raise CredentialDecodeError from None
