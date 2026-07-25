"""Strict Codex payload schemas and provider-native time conversion."""

from datetime import UTC, date, datetime, timedelta
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

from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    CodexCredentials,
    DetectedCredentials,
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.models import CodexTokenClaims
from sidekick_usages.providers.codex.token import (
    decode_codex_token_claims,
    validated_codex_token,
)
from sidekick_usages.serialization.json import JsonObject

_MAX_METADATA_BYTES = 4_096
_MAX_PLAN_BYTES = 256
_MAX_TIMESTAMP_BYTES = 4_096
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
_SAFE_PATH_SEGMENTS = frozenset(
    {
        "access_token",
        "account_id",
        "additional_rate_limits",
        "auth",
        "exp",
        "expires_in",
        "id_token",
        "label",
        "last_refresh",
        "lifetime_tokens",
        "limit_name",
        "longest_running_turn_sec",
        "longest_streak_days",
        "model",
        "peak_daily_tokens",
        "plan",
        "plan_type",
        "primary_window",
        "rate_limit",
        "refresh_token",
        "reset_at",
        "resets_at",
        "secondary_window",
        "start_date",
        "stats",
        "tokens",
        "current_streak_days",
        "daily_usage_buckets",
        "used_percent",
    }
)


type _TokenString = Annotated[
    str,
    AfterValidator(validated_codex_token),
]
type _MetadataString = Annotated[str, AfterValidator(_metadata)]
type _PlanString = Annotated[str, AfterValidator(_plan)]
type _OpaqueTimestamp = Annotated[str, AfterValidator(_timestamp)]
type _Utilization = Annotated[
    int | float,
    Field(ge=0, le=100, allow_inf_nan=False),
]
type _Epoch = Annotated[int | float, Field(ge=0, allow_inf_nan=False)]


def _bounded_utf8(value: str, maximum: int) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError from error
    if not encoded or len(encoded) > maximum:
        raise ValueError
    return value


def _metadata(value: str) -> str:
    return _bounded_utf8(value, _MAX_METADATA_BYTES)


def _plan(value: str) -> str:
    return _bounded_utf8(value, _MAX_PLAN_BYTES)


def _timestamp(value: str) -> str:
    return _bounded_utf8(value, _MAX_TIMESTAMP_BYTES)


class _ProviderModel(BaseModel):
    """Strict provider model that intentionally tolerates new fields."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class _AuthTokensSchema(_ProviderModel):
    access_token: _TokenString = Field(repr=False)
    refresh_token: _TokenString | None = Field(default=None, repr=False)
    id_token: _TokenString | None = Field(default=None, repr=False)
    account_id: _MetadataString | None = None


class _AuthDocumentSchema(_ProviderModel):
    tokens: _AuthTokensSchema
    last_refresh: _OpaqueTimestamp | None = None


class _AuthIdentityTokensSchema(_ProviderModel):
    access_token: _TokenString | None = Field(default=None, repr=False)
    account_id: _MetadataString | None = None


class _AuthIdentityDocumentSchema(_ProviderModel):
    tokens: _AuthIdentityTokensSchema


class _UsageWindowSchema(_ProviderModel):
    used_percent: _Utilization
    resets_at: _MetadataString | None = None
    reset_at: _Epoch | None = None


class _RateLimitSchema(_ProviderModel):
    primary_window: _UsageWindowSchema | None = None
    secondary_window: _UsageWindowSchema | None = None


class _AdditionalRateLimitSchema(_ProviderModel):
    limit_name: _MetadataString | None = None
    label: _MetadataString | None = None
    model: _MetadataString | None = None
    rate_limit: _RateLimitSchema | None = None
    used_percent: _Utilization | None = None
    resets_at: _MetadataString | None = None
    reset_at: _Epoch | None = None


class _UsageResponseSchema(_ProviderModel):
    plan_type: _PlanString | None = None
    plan: _PlanString | None = None
    rate_limit: _RateLimitSchema | None = None
    primary_window: _UsageWindowSchema | None = None
    secondary_window: _UsageWindowSchema | None = None
    additional_rate_limits: list[_AdditionalRateLimitSchema] = Field(
        default_factory=list
    )


class _TokenUsageDailyBucketSchema(_ProviderModel):
    start_date: _MetadataString
    tokens: int = Field(ge=0, le=_MAX_TOKEN_COUNT)


class _TokenUsageStatsSchema(_ProviderModel):
    lifetime_tokens: int = Field(ge=0, le=_MAX_TOKEN_COUNT)
    peak_daily_tokens: int | None = Field(
        default=None,
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )
    longest_running_turn_sec: int | None = Field(default=None, ge=0)
    current_streak_days: int | None = Field(default=None, ge=0)
    longest_streak_days: int | None = Field(default=None, ge=0)
    daily_usage_buckets: list[_TokenUsageDailyBucketSchema] | None = None


class _TokenUsageProfileSchema(_ProviderModel):
    stats: _TokenUsageStatsSchema


@cache
def _auth_adapter() -> TypeAdapter[_AuthDocumentSchema]:
    return TypeAdapter(_AuthDocumentSchema)


@cache
def _auth_identity_adapter() -> TypeAdapter[_AuthIdentityDocumentSchema]:
    return TypeAdapter(_AuthIdentityDocumentSchema)


@cache
def _usage_adapter() -> TypeAdapter[_UsageResponseSchema]:
    return TypeAdapter(_UsageResponseSchema)


@cache
def _token_usage_profile_adapter() -> TypeAdapter[_TokenUsageProfileSchema]:
    return TypeAdapter(_TokenUsageProfileSchema)


def _failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    fields: tuple[str, ...] = (),
) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
        fields=fields,
    )


def _validation_kind(error: ValidationError) -> ProviderFailureKind:
    details = error.errors(include_input=False, include_url=False)
    if any(detail["type"] == "missing" for detail in details):
        return ProviderFailureKind.INCOMPLETE
    return ProviderFailureKind.MALFORMED


def _safe_paths(error: ValidationError) -> tuple[str, ...]:
    paths: list[str] = []
    for detail in error.errors(include_input=False, include_url=False):
        segments = tuple(
            segment
            for item in detail["loc"]
            if (segment := _safe_path_segment(item)) is not None
        )
        rendered = ".".join(str(segment) for segment in segments)
        if rendered and rendered not in paths:
            paths.append(rendered)
    return tuple(paths)


def _safe_path_segment(segment: str | int) -> str | int | None:
    if isinstance(segment, int):
        return segment
    if segment == "https://api.openai.com/auth":
        return "auth"
    if segment in _SAFE_PATH_SEGMENTS:
        return segment
    return None


def _validate[T](
    adapter: TypeAdapter[T],
    value: object,
    *,
    message: str,
) -> T:
    try:
        return adapter.validate_python(value, strict=True)
    except ValidationError as error:
        fields = _safe_paths(error)
        raise ProviderBoundaryError(
            _failure(
                _validation_kind(error),
                message,
                fields=fields,
            )
        ) from None


def _token_claims(token: str) -> CodexTokenClaims:
    try:
        return decode_codex_token_claims(token)
    except TypeError, ValueError:
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex access-token metadata is malformed; log in again.",
            )
        ) from None


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
            _failure(
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
    document = _validate(
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
                _failure(
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
            _failure(
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
            _failure(
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
    """Return the auth document's exact access token and consistent id."""
    document = _validate(
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
            _failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                "Codex auth.json contains conflicting account identities.",
                fields=("tokens.account_id", "auth.chatgpt_account_id"),
            )
        ) from None
    return declared_id or claims_id


def parse_usage_response(value: JsonObject) -> UsageReport:
    """Validate and normalize one Codex usage response."""
    response = _validate(
        _usage_adapter(),
        value,
        message="Codex returned an invalid usage response.",
    )
    rate_limit = response.rate_limit or _RateLimitSchema(
        primary_window=response.primary_window,
        secondary_window=response.secondary_window,
    )
    windows = _rate_limit_windows(rate_limit)
    for extra in response.additional_rate_limits:
        label = extra.limit_name or extra.label or extra.model
        if label is None:
            raise ProviderBoundaryError(
                _failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Codex returned an unnamed additional usage limit.",
                )
            )
        if extra.rate_limit is not None:
            windows.extend(_rate_limit_windows(extra.rate_limit, label))
            continue
        if extra.used_percent is None:
            raise ProviderBoundaryError(
                _failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Codex returned an incomplete additional usage limit.",
                )
            )
        windows.append(
            _usage_window(
                label,
                _UsageWindowSchema(
                    used_percent=extra.used_percent,
                    resets_at=extra.resets_at,
                    reset_at=extra.reset_at,
                ),
            )
        )
    if not windows:
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.INCOMPLETE,
                "Codex returned no supported usage windows.",
            )
        )
    return UsageReport(
        windows=tuple(windows),
        plan=response.plan_type or response.plan,
    )


def parse_activity_response(value: JsonObject) -> TokenActivitySummary:
    """Validate and normalize one Codex account activity profile."""
    profile = _validate(
        _token_usage_profile_adapter(),
        value,
        message="Codex token activity response is incomplete or malformed.",
    )
    seen_dates: set[date] = set()
    bucket_total = 0
    bucket_overflow = False
    for bucket in profile.stats.daily_usage_buckets or ():
        try:
            bucket_date = date.fromisoformat(bucket.start_date)
        except ValueError:
            raise ProviderBoundaryError(
                _failure(
                    ProviderFailureKind.MALFORMED,
                    "Codex token activity contains an invalid bucket date.",
                    fields=("stats.daily_usage_buckets.start_date",),
                )
            ) from None
        if bucket_date in seen_dates:
            raise ProviderBoundaryError(
                _failure(
                    ProviderFailureKind.MALFORMED,
                    "Codex token activity contains duplicate bucket dates.",
                    fields=("stats.daily_usage_buckets.start_date",),
                )
            )
        seen_dates.add(bucket_date)
        if bucket.tokens > _MAX_TOKEN_COUNT - bucket_total:
            bucket_overflow = True
        elif not bucket_overflow:
            bucket_total += bucket.tokens
    since = (
        min(seen_dates)
        if seen_dates
        and not bucket_overflow
        and bucket_total == profile.stats.lifetime_tokens
        else None
    )
    return TokenActivitySummary(
        total_tokens=profile.stats.lifetime_tokens,
        scope=TokenActivityScope.ACCOUNT,
        since=since,
    )


def _rate_limit_windows(
    rate_limit: _RateLimitSchema,
    prefix: str | None = None,
) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    if rate_limit.primary_window is not None:
        name = "5h" if prefix is None else f"{prefix} 5h"
        windows.append(_usage_window(name, rate_limit.primary_window))
    if rate_limit.secondary_window is not None:
        name = "7d" if prefix is None else f"{prefix} 7d"
        windows.append(_usage_window(name, rate_limit.secondary_window))
    return windows


def _usage_window(name: str, window: _UsageWindowSchema) -> UsageWindow:
    resets_at = _provider_time(window.resets_at)
    if resets_at is None and window.reset_at is not None:
        resets_at = _epoch_time(window.reset_at)
    return UsageWindow(
        name=name,
        utilization=float(window.used_percent),
        resets_at=resets_at,
    )


def _provider_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned a timezone-free usage reset timestamp.",
            )
        )
    return parsed.astimezone(UTC)


def _epoch_time(value: int | float) -> datetime:
    if isinstance(value, bool):
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OverflowError, OSError, ValueError:
        raise ProviderBoundaryError(
            _failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        ) from None
