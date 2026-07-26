"""Strict Codex authentication schemas and token normalization."""

from datetime import UTC, datetime, timedelta
from functools import cache

from pydantic import Field, TypeAdapter

from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import CodexCredentials, DetectedCredentials
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.auth.models import CodexTokenClaims
from sidekick_usages.providers.codex.auth.token import (
    decode_codex_token_claims,
)
from sidekick_usages.providers.codex.failures import codex_failure
from sidekick_usages.providers.codex.schema.validation import (
    MetadataString,
    OpaqueTimestamp,
    ProviderSchema,
    TokenString,
    validate_provider_payload,
)
from sidekick_usages.serialization.json import JsonObject


class AuthTokensSchema(ProviderSchema):
    """Strict token fields from one Codex auth document."""

    access_token: TokenString = Field(repr=False)
    refresh_token: TokenString | None = Field(default=None, repr=False)
    id_token: TokenString | None = Field(default=None, repr=False)
    account_id: MetadataString | None = None


class AuthDocumentSchema(ProviderSchema):
    """Strict Codex auth document."""

    tokens: AuthTokensSchema
    last_refresh: OpaqueTimestamp | None = None


class AuthIdentityTokensSchema(ProviderSchema):
    """Narrow identity-bearing token fields."""

    access_token: TokenString | None = Field(default=None, repr=False)
    account_id: MetadataString | None = None


class AuthIdentityDocumentSchema(ProviderSchema):
    """Narrow Codex auth identity document."""

    tokens: AuthIdentityTokensSchema


@cache
def _auth_adapter() -> TypeAdapter[AuthDocumentSchema]:
    return TypeAdapter(AuthDocumentSchema)


@cache
def _auth_identity_adapter() -> TypeAdapter[AuthIdentityDocumentSchema]:
    return TypeAdapter(AuthIdentityDocumentSchema)


def jwt_expiry(token: str) -> Expiry:
    """Normalize a Codex JWT expiry to an aware UTC value."""
    claims = _token_claims(token)
    if claims.expiry_seconds is None:
        return UnknownExpiry()
    try:
        at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            seconds=claims.expiry_seconds
        )
    except OverflowError:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex access-token expiry is outside the supported range.",
            )
        ) from None
    return KnownExpiry(at)


def account_id_from_token(token: str) -> str | None:
    """Return the ChatGPT account id carried by a validated token."""
    identity = _token_claims(token).provider_identity
    return None if identity is None else str(identity)


def plan_from_token(token: str) -> str | None:
    """Return the plan carried by a validated Codex token."""
    return _token_claims(token).plan


def parse_auth_credentials(blob: JsonObject) -> DetectedCredentials:
    """Validate a Codex auth document and construct runtime credentials."""
    document = validate_provider_payload(
        _auth_adapter(),
        blob,
        message="Codex auth.json is incomplete or malformed; log in again.",
    )
    for field_name, field_value in (
        ("refresh_token", document.tokens.refresh_token),
        ("id_token", document.tokens.id_token),
    ):
        if (
            field_name in document.tokens.model_fields_set
            and field_value is None
        ):
            raise ProviderBoundaryError(
                codex_failure(
                    ProviderFailureKind.MALFORMED,
                    "Codex auth.json contains invalid token metadata.",
                    fields=(f"tokens.{field_name}",),
                )
            ) from None
    claims = _token_claims(document.tokens.access_token)
    claims_id = (
        None
        if claims.provider_identity is None
        else str(claims.provider_identity)
    )
    account_id = _consistent_account_id(
        document.tokens.account_id,
        claims_id,
    )
    if account_id is None:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.INCOMPLETE,
                "Codex auth.json contains no account identity; log in again.",
                fields=("tokens.account_id",),
            )
        )
    plan = claims.plan
    return DetectedCredentials(
        credentials=CodexCredentials(
            access_token=document.tokens.access_token,
            account_id=account_id,
            refresh_token=document.tokens.refresh_token,
            expiry=jwt_expiry(document.tokens.access_token),
            id_token=document.tokens.id_token,
            auth_last_refresh=document.last_refresh,
        ),
        plan=plan or "unknown",
    )


def credentials_from_access_token(token: str) -> DetectedCredentials:
    """Construct Codex credentials from one validated access token."""
    claims = _token_claims(token)
    account_id = (
        None
        if claims.provider_identity is None
        else str(claims.provider_identity)
    )
    if account_id is None:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.INCOMPLETE,
                "The Codex token contains no account identity; log in again.",
                fields=("auth.chatgpt_account_id",),
            )
        )
    plan = claims.plan
    return DetectedCredentials(
        credentials=CodexCredentials(
            access_token=token,
            account_id=account_id,
            expiry=jwt_expiry(token),
        ),
        plan=plan or "unknown",
    )


def auth_blob_account_id(blob: JsonObject) -> str | None:
    """Return an account id from a narrowly validated auth document."""
    _, account_id = _auth_blob_identity(blob)
    return account_id


def auth_blob_access_token(blob: JsonObject) -> str | None:
    """Return the access token from a narrowly validated auth document."""
    access_token, _ = _auth_blob_identity(blob)
    return access_token


def _auth_blob_identity(
    blob: JsonObject,
) -> tuple[str | None, str | None]:
    document = validate_provider_payload(
        _auth_identity_adapter(),
        blob,
        message="Codex auth.json identity metadata is malformed.",
    )
    access_token = document.tokens.access_token
    claims_id = (
        None if access_token is None else account_id_from_token(access_token)
    )
    return (
        access_token,
        _consistent_account_id(document.tokens.account_id, claims_id),
    )


def _consistent_account_id(
    declared_id: str | None,
    claims_id: str | None,
) -> str | None:
    if (
        declared_id is not None
        and claims_id is not None
        and declared_id != claims_id
    ):
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                "Codex auth.json contains conflicting account identities.",
                fields=("tokens.account_id", "auth.chatgpt_account_id"),
            )
        ) from None
    return declared_id or claims_id


def _token_claims(token: str) -> CodexTokenClaims:
    try:
        return decode_codex_token_claims(token)
    except TypeError, ValueError:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex access-token metadata is malformed; log in again.",
            )
        ) from None
