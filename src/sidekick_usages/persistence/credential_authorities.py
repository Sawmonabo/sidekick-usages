"""Strict protected legacy credential authorities keyed by stable IDs."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.accounts import (
    AuthorityId,
    ClaudeAccountAuthority,
    ClaudeLegacyLoginAuthority,
    CodexLegacyAuthority,
    SavedAccount,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    MAX_DOCUMENT_BYTES,
    _canonical_timestamp,
    _parse_canonical_timestamp,
)
from sidekick_usages.serialization import (
    JsonDecodeError,
    JsonObject,
    JsonValue,
    decode_json_value,
)

AUTHORITY_BASENAME = "authority.json"
AUTHORITY_SCHEMA_VERSION = 1
_MAX_METADATA_BYTES = 4_096

_MODEL_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class CredentialAuthorityKind(StrEnum):
    """Closed protected legacy credential variants."""

    CLAUDE_SETUP_TOKEN = "claude_setup_token"
    CLAUDE_SUBSCRIPTION = "claude_subscription"
    CODEX_SUBSCRIPTION = "codex_subscription"


def _canonical_uuid(value: str) -> str:
    """Validate a canonical stable identifier."""
    SidekickAccountId(value)
    return value


def _timestamp(value: str) -> str:
    """Validate one canonical persisted timestamp."""
    _parse_canonical_timestamp(value)
    return value


def _secret(value: str) -> str:
    """Require one bounded nonempty credential string."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Credential material must be valid UTF-8.") from None
    if not encoded or len(encoded) > 1024 * 1024:
        raise ValueError("Credential material must be nonempty and bounded.")
    return value


def _metadata(value: str) -> str:
    """Require one bounded nonempty metadata string."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Credential metadata must be valid UTF-8.") from None
    if not encoded or len(encoded) > _MAX_METADATA_BYTES:
        raise ValueError("Credential metadata must be nonempty and bounded.")
    return value


type _Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
type _Timestamp = Annotated[str, AfterValidator(_timestamp)]
type _Secret = Annotated[str, AfterValidator(_secret)]
type _Metadata = Annotated[str, AfterValidator(_metadata)]


class _AuthorityBaseModel(BaseModel):
    """Fields binding protected credentials to one logical account."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1]
    authority_id: _Uuid
    account_id: _Uuid


class _ClaudeSetupModel(_AuthorityBaseModel):
    """Strict protected Claude setup-token authority."""

    provider_id: Literal["claude"]
    credential_kind: Literal["claude_setup_token"]
    access_token: _Secret


class _ClaudeIdentityModel(BaseModel):
    """Strict complete Claude provider identity."""

    model_config = _MODEL_CONFIG

    account_id: _Metadata
    organization_id: _Metadata


class _ClaudeSubscriptionModel(_AuthorityBaseModel):
    """Strict protected Claude subscription authority."""

    provider_id: Literal["claude"]
    credential_kind: Literal["claude_subscription"]
    access_token: _Secret
    refresh_token: _Secret
    access_expires_at: _Timestamp
    refresh_expires_at: _Timestamp | None
    scopes: list[_Metadata] = Field(min_length=1, max_length=128)
    identity: _ClaudeIdentityModel | None


class _CodexSubscriptionModel(_AuthorityBaseModel):
    """Strict protected Codex subscription authority."""

    provider_id: Literal["codex"]
    credential_kind: Literal["codex_subscription"]
    access_token: _Secret
    refresh_token: _Secret | None
    expires_at: _Timestamp | None
    provider_account_id: _Metadata | None
    auth_home: _Metadata | None
    id_token: _Secret | None
    auth_last_refresh: _Metadata | None


type _AuthorityModel = Annotated[
    _ClaudeSetupModel | _ClaudeSubscriptionModel | _CodexSubscriptionModel,
    Field(discriminator="credential_kind"),
]

_AUTHORITY_ADAPTER = TypeAdapter(_AuthorityModel)


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyCredentialAuthority:
    """One protected legacy credential authority with a redacted repr."""

    authority_id: AuthorityId
    account_id: SidekickAccountId
    provider_id: ProviderId
    kind: CredentialAuthorityKind
    credentials: Credentials = field(repr=False)

    def __post_init__(self) -> None:
        """Require the credential and authority provider to agree."""
        if self.credentials.provider_id is not self.provider_id:
            raise ValueError("Credential authority provider does not match.")
        if (
            self.kind is CredentialAuthorityKind.CLAUDE_SETUP_TOKEN
            and not isinstance(
                self.credentials,
                ClaudeSetupTokenCredentials,
            )
        ):
            raise ValueError("Claude setup authority kind does not match.")
        if (
            self.kind is CredentialAuthorityKind.CLAUDE_SUBSCRIPTION
            and not isinstance(self.credentials, ClaudeLoginCredentials)
        ):
            raise ValueError(
                "Claude subscription authority kind does not match."
            )
        if (
            self.kind is CredentialAuthorityKind.CODEX_SUBSCRIPTION
            and not isinstance(self.credentials, CodexCredentials)
        ):
            raise ValueError("Codex authority kind does not match.")


def authority_for_account(
    account: Account,
    *,
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
) -> LegacyCredentialAuthority:
    """Bind one current account's credential material to stable IDs."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        kind = CredentialAuthorityKind.CLAUDE_SETUP_TOKEN
    elif isinstance(credentials, ClaudeLoginCredentials):
        kind = CredentialAuthorityKind.CLAUDE_SUBSCRIPTION
    else:
        kind = CredentialAuthorityKind.CODEX_SUBSCRIPTION
    return LegacyCredentialAuthority(
        authority_id=authority_id,
        account_id=account_id,
        provider_id=account.provider_id,
        kind=kind,
        credentials=credentials,
    )


def _optional_time(value: datetime | None) -> JsonValue:
    """Encode an optional canonical timestamp."""
    return None if value is None else _canonical_timestamp(value)


def _authority_object(authority: LegacyCredentialAuthority) -> JsonObject:
    """Encode one protected authority without exposing it in reprs."""
    credentials = authority.credentials
    common: JsonObject = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "authority_id": str(authority.authority_id),
        "account_id": str(authority.account_id),
        "provider_id": authority.provider_id.value,
        "credential_kind": authority.kind.value,
    }
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return {
            **common,
            "access_token": credentials.access_token,
        }
    if isinstance(credentials, ClaudeLoginCredentials):
        identity = credentials.identity
        return {
            **common,
            "access_token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
            "access_expires_at": _canonical_timestamp(
                credentials.access_expiry.at
            ),
            "refresh_expires_at": (
                _canonical_timestamp(credentials.refresh_expiry.at)
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
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "expires_at": (
            _canonical_timestamp(credentials.expiry.at)
            if isinstance(credentials.expiry, KnownExpiry)
            else None
        ),
        "provider_account_id": credentials.account_id,
        "auth_home": credentials.auth_home,
        "id_token": credentials.id_token,
        "auth_last_refresh": credentials.auth_last_refresh,
    }


def encode_credential_authority(
    authority: LegacyCredentialAuthority,
) -> bytes:
    """Encode one strict protected legacy credential authority."""
    try:
        payload = (
            json.dumps(
                _authority_object(authority),
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
    if decode_credential_authority(payload) != authority:
        raise InvalidSchemaError
    return payload


def _decoded_authority(
    model: _ClaudeSetupModel
    | _ClaudeSubscriptionModel
    | _CodexSubscriptionModel,
) -> LegacyCredentialAuthority:
    """Convert one validated protected model to core credentials."""
    if isinstance(model, _ClaudeSetupModel):
        credentials: Credentials = ClaudeSetupTokenCredentials(
            access_token=model.access_token
        )
        kind = CredentialAuthorityKind.CLAUDE_SETUP_TOKEN
    elif isinstance(model, _ClaudeSubscriptionModel):
        credentials = ClaudeLoginCredentials(
            access_token=model.access_token,
            refresh_token=model.refresh_token,
            access_expiry=KnownExpiry(
                _parse_canonical_timestamp(model.access_expires_at)
            ),
            refresh_expiry=(
                KnownExpiry(
                    _parse_canonical_timestamp(model.refresh_expires_at)
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
        kind = CredentialAuthorityKind.CLAUDE_SUBSCRIPTION
    else:
        credentials = CodexCredentials(
            access_token=model.access_token,
            refresh_token=model.refresh_token,
            expiry=(
                KnownExpiry(_parse_canonical_timestamp(model.expires_at))
                if model.expires_at is not None
                else UnknownExpiry()
            ),
            account_id=model.provider_account_id,
            auth_home=model.auth_home,
            id_token=model.id_token,
            auth_last_refresh=model.auth_last_refresh,
        )
        kind = CredentialAuthorityKind.CODEX_SUBSCRIPTION
    return LegacyCredentialAuthority(
        authority_id=AuthorityId(model.authority_id),
        account_id=SidekickAccountId(model.account_id),
        provider_id=ProviderId(model.provider_id),
        kind=kind,
        credentials=credentials,
    )


def decode_credential_authority(payload: bytes) -> LegacyCredentialAuthority:
    """Decode one strict protected legacy credential authority."""
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    try:
        value = decode_json_value(payload)
        model = _AUTHORITY_ADAPTER.validate_python(value, strict=True)
        return _decoded_authority(model)
    except JsonDecodeError, ValidationError, TypeError, ValueError:
        raise InvalidSchemaError from None


def authority_bundle_name(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
) -> str:
    """Return the qualified direct bundle name for one authority."""
    return f"{account_id}--{authority_id}"


def referenced_legacy_authorities(
    account: SavedAccount,
) -> tuple[AuthorityId, ...]:
    """Return every protected legacy authority owned by one account."""
    authority = account.authority
    references: list[AuthorityId] = []
    if isinstance(authority, ClaudeAccountAuthority):
        if authority.setup_token is not None:
            references.append(authority.setup_token.authority_id)
        if isinstance(authority.subscription, ClaudeLegacyLoginAuthority):
            references.append(authority.subscription.authority_id)
    elif isinstance(authority.subscription, CodexLegacyAuthority):
        references.append(authority.subscription.authority_id)
    return tuple(references)


class CredentialAuthorityRepository:
    """Qualified protected legacy authority storage."""

    def __init__(self, tree: PrivateCredentialTree) -> None:
        self.tree = tree

    def bundle_path(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> Path:
        """Return one direct protected bundle derived only from stable IDs."""
        return self.tree.root / authority_bundle_name(
            account_id,
            authority_id,
        )

    def prepare_write(
        self,
        authority: LegacyCredentialAuthority,
        *,
        expected_payload: bytes | None = None,
    ) -> PreparedPrivateBundleWrite:
        """Prepare one coordinated protected authority write."""
        return PreparedPrivateBundleWrite(
            path=self.bundle_path(
                authority.account_id,
                authority.authority_id,
            ),
            files={AUTHORITY_BASENAME: encode_credential_authority(authority)},
            expected_bundle_present=expected_payload is not None,
            expected_files=(
                {AUTHORITY_BASENAME: expected_payload}
                if expected_payload is not None
                else {}
            ),
        )

    def read_payload(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> bytes | None:
        """Read exact protected bytes for one qualified authority."""
        bundle = self.bundle_path(account_id, authority_id)
        return self.tree.read_bundle_file(bundle, AUTHORITY_BASENAME)

    def read(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> LegacyCredentialAuthority | None:
        """Read and rebind one exact protected authority."""
        payload = self.read_payload(account_id, authority_id)
        if payload is None:
            return None
        authority = decode_credential_authority(payload)
        if (
            authority.account_id != account_id
            or authority.authority_id != authority_id
        ):
            raise InvalidSchemaError
        return authority


__all__ = [
    "AUTHORITY_BASENAME",
    "CredentialAuthorityKind",
    "CredentialAuthorityRepository",
    "LegacyCredentialAuthority",
    "authority_bundle_name",
    "authority_for_account",
    "decode_credential_authority",
    "encode_credential_authority",
    "referenced_legacy_authorities",
]
