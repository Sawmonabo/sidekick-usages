"""Strict Claude credential schemas and normalization."""

import re
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    DetectedCredentials,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.schema.usage import (
    _bounded_string,
    _validate,
    claude_failure,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue

CLAUDE_TOKEN_PATTERN = re.compile(
    r"sk-ant-oat01-[A-Za-z0-9_-]+",
    re.ASCII,
)
PROFILE_SCOPE = "user:profile"

_MAX_TOKEN_BYTES = 262_144
_MAX_METADATA_BYTES = 4_096
_MAX_PLAN_BYTES = 256
_MAX_SCOPES = 128
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


type _Token = Annotated[str, AfterValidator(_token)]
type _Metadata = Annotated[str, AfterValidator(_metadata)]
type _Plan = Annotated[str, AfterValidator(_plan)]
type _Scopes = Annotated[list[_Metadata], AfterValidator(_scopes)]
type _NonnegativeInteger = Annotated[
    int,
    AfterValidator(_nonnegative_integer),
]


def _token(value: str) -> str:
    return _bounded_string(value, _MAX_TOKEN_BYTES)


def _metadata(value: str) -> str:
    return _bounded_string(value, _MAX_METADATA_BYTES)


def _plan(value: str) -> str:
    return _bounded_string(value, _MAX_PLAN_BYTES)


def _scopes(value: list[str]) -> list[str]:
    if (
        not value
        or len(value) > _MAX_SCOPES
        or len(value) != len(set(value))
        or PROFILE_SCOPE not in value
    ):
        raise ValueError
    return value


def _nonnegative_integer(value: int) -> int:
    if value < 0:
        raise ValueError
    return value


class _ClaudeTokenAccount(BaseModel):
    """Optional stable identity embedded in a Claude login envelope."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    account_id: _Metadata | None = Field(
        default=None,
        alias="accountUuid",
        repr=False,
    )
    organization_id: _Metadata | None = Field(
        default=None,
        alias="organizationUuid",
        repr=False,
    )


class _ClaudeOAuthCredentials(BaseModel):
    """Strict model for complete Claude subscription-login state."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    access_token: _Token = Field(alias="accessToken", repr=False)
    refresh_token: _Token = Field(alias="refreshToken", repr=False)
    access_expires_at: _NonnegativeInteger = Field(alias="expiresAt")
    refresh_expires_at: _NonnegativeInteger | None = Field(
        default=None,
        alias="refreshTokenExpiresAt",
    )
    subscription_type: _Plan | None = Field(
        default=None,
        alias="subscriptionType",
    )
    scopes: _Scopes
    token_account: _ClaudeTokenAccount | None = Field(
        default=None,
        alias="tokenAccount",
        repr=False,
    )


class _ClaudeCredentialsEnvelope(BaseModel):
    """Strict model for Claude's native credential envelope."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    oauth: _ClaudeOAuthCredentials = Field(alias="claudeAiOauth")


class _ClaudeRefreshResponse(BaseModel):
    """Strict model for Claude's refresh response contract."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    access_token: _Token = Field(repr=False)
    refresh_token: _Token | None = Field(default=None, repr=False)
    expires_in: _NonnegativeInteger
    refresh_token_expires_in: _NonnegativeInteger | None = None


@cache
def _credentials_adapter() -> TypeAdapter[_ClaudeCredentialsEnvelope]:
    return TypeAdapter(_ClaudeCredentialsEnvelope)


@cache
def _refresh_adapter() -> TypeAdapter[_ClaudeRefreshResponse]:
    return TypeAdapter(_ClaudeRefreshResponse)


@cache
def _expires_in_adapter() -> TypeAdapter[_NonnegativeInteger]:
    return TypeAdapter(
        _NonnegativeInteger,
        config=ConfigDict(strict=True),
    )


@cache
def _setup_token_adapter() -> TypeAdapter[str]:
    return TypeAdapter(
        Annotated[str, AfterValidator(_token)],
        config=ConfigDict(strict=True),
    )


def _timestamp_expiry(value: int, field: str) -> KnownExpiry:
    try:
        return KnownExpiry(_EPOCH + timedelta(milliseconds=value))
    except OverflowError:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                f"Claude credential data is invalid at {field}.",
                fields=(field,),
            )
        ) from None


def _login_identity(
    token_account: _ClaudeTokenAccount | None,
) -> ClaudeLoginIdentity | None:
    if token_account is None:
        return None
    account_id = token_account.account_id
    organization_id = token_account.organization_id
    if account_id is None and organization_id is None:
        return None
    if account_id is None or organization_id is None:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.INCOMPLETE,
                "Claude credential identity is incomplete.",
                fields=("tokenAccount",),
            )
        ) from None
    return ClaudeLoginIdentity(
        account_id=account_id,
        organization_id=organization_id,
    )


def parse_credentials_blob(blob: JsonObject) -> DetectedCredentials:
    """Validate and normalize complete Claude Code login credentials."""
    validated = _validate(
        _credentials_adapter(),
        blob,
        boundary="credential",
    )
    oauth = validated.oauth
    raw_oauth = blob.get("claudeAiOauth")
    if (
        isinstance(raw_oauth, dict)
        and "refreshTokenExpiresAt" in raw_oauth
        and oauth.refresh_expires_at is None
    ):
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude credential data is invalid at refreshTokenExpiresAt.",
                fields=("refreshTokenExpiresAt",),
            )
        ) from None
    refresh_expiry = (
        UnknownExpiry()
        if oauth.refresh_expires_at is None
        else _timestamp_expiry(
            oauth.refresh_expires_at,
            "refreshTokenExpiresAt",
        )
    )
    return DetectedCredentials(
        credentials=ClaudeLoginCredentials(
            access_token=oauth.access_token,
            refresh_token=oauth.refresh_token,
            access_expiry=_timestamp_expiry(
                oauth.access_expires_at,
                "expiresAt",
            ),
            refresh_expiry=refresh_expiry,
            scopes=tuple(oauth.scopes),
            identity=_login_identity(oauth.token_account),
        ),
        plan=oauth.subscription_type or "unknown",
    )


def claude_expiry(value: JsonValue | None) -> Expiry:
    """Normalize one raw Claude millisecond timestamp."""
    if value is None:
        return UnknownExpiry()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return InvalidExpiry()
    try:
        return KnownExpiry(_EPOCH + timedelta(milliseconds=value))
    except OverflowError:
        return InvalidExpiry()


def refresh_expiry(
    value: JsonValue,
    reference_time: datetime,
) -> KnownExpiry:
    """Normalize one strict Claude refresh-relative expiry duration."""
    validated = _validate(
        _expires_in_adapter(),
        value,
        boundary="refresh expiry",
    )
    normalized = as_utc(reference_time)
    normalized = normalized.replace(
        microsecond=(normalized.microsecond // 1000) * 1000
    )
    try:
        return KnownExpiry(normalized + timedelta(seconds=validated))
    except OverflowError:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude refresh expiry is outside the supported range.",
            )
        ) from None


def parse_refresh_credentials(
    value: JsonObject,
    previous: ClaudeLoginCredentials,
    reference_time: datetime,
) -> ClaudeLoginCredentials:
    """Validate a refresh response and build complete login credentials."""
    validated = _validate(
        _refresh_adapter(),
        value,
        boundary="refresh",
    )
    if "refresh_token" in value and validated.refresh_token is None:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude refresh data is invalid at refresh_token.",
                fields=("refresh_token",),
            )
        ) from None
    if (
        "refresh_token_expires_in" in value
        and validated.refresh_token_expires_in is None
    ):
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude refresh data is invalid at refresh_token_expires_in.",
                fields=("refresh_token_expires_in",),
            )
        ) from None
    next_refresh_expiry = previous.refresh_expiry
    if validated.refresh_token_expires_in is not None:
        next_refresh_expiry = refresh_expiry(
            validated.refresh_token_expires_in,
            reference_time,
        )
    return ClaudeLoginCredentials(
        access_token=validated.access_token,
        refresh_token=validated.refresh_token or previous.refresh_token,
        access_expiry=refresh_expiry(
            validated.expires_in,
            reference_time,
        ),
        refresh_expiry=next_refresh_expiry,
        scopes=previous.scopes,
        identity=previous.identity,
    )


def validate_setup_token(value: str) -> str:
    """Validate one token captured from Claude's setup-token process."""
    try:
        validated = _setup_token_adapter().validate_python(value, strict=True)
    except ValidationError:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude setup-token returned an invalid token.",
            )
        ) from None
    if CLAUDE_TOKEN_PATTERN.fullmatch(validated) is None:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude setup-token returned an invalid token.",
            )
        ) from None
    return validated
