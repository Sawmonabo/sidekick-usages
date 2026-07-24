"""Strict Claude usage schemas and normalization."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.models import UsageWindow
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
)
from sidekick_usages.serialization import JsonObject, JsonValue

_MAX_METADATA_BYTES = 4_096
_MAX_UTILIZATION_PERCENT = 100
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
_SAFE_PATH_SEGMENTS = frozenset(
    {
        "accessToken",
        "accountUuid",
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "claudeAiOauth",
        "expiresAt",
        "expires_in",
        "five_hour",
        "firstSessionDate",
        "inputTokens",
        "input_tokens",
        "isSidechain",
        "lastComputedDate",
        "message",
        "modelUsage",
        "outputTokens",
        "output_tokens",
        "refreshToken",
        "refreshTokenExpiresAt",
        "refresh_token",
        "refresh_token_expires_in",
        "resets_at",
        "scopes",
        "seven_day",
        "seven_day_oauth_apps",
        "seven_day_opus",
        "subscriptionType",
        "tokenAccount",
        "organizationUuid",
        "timestamp",
        "type",
        "usage",
        "utilization",
    }
)
_OAUTH_USAGE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_oauth_apps", "7d OAuth"),
)


def _bounded_string(value: str, maximum: int) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError from error
    if not encoded or len(encoded) > maximum:
        raise ValueError
    return value


def _metadata(value: str) -> str:
    return _bounded_string(value, _MAX_METADATA_BYTES)


def _utilization(value: int | float) -> int | float:
    try:
        numeric = float(value)
    except OverflowError as error:
        raise ValueError from error
    if (
        not math.isfinite(numeric)
        or not 0 <= numeric <= _MAX_UTILIZATION_PERCENT
    ):
        raise ValueError
    return value


type _Metadata = Annotated[str, AfterValidator(_metadata)]
type _Utilization = Annotated[
    int | float,
    AfterValidator(_utilization),
]


class _ClaudeUsageWindow(BaseModel):
    """Private strict model for one Claude OAuth usage window."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    utilization: _Utilization
    resets_at: _Metadata | None


class _ClaudeUsageResponse(BaseModel):
    """Private strict model for Claude's OAuth usage response."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    five_hour: _ClaudeUsageWindow | None = None
    seven_day: _ClaudeUsageWindow | None = None
    seven_day_opus: _ClaudeUsageWindow | None = None
    seven_day_oauth_apps: _ClaudeUsageWindow | None = None


class _ClaudeHeaderWindow(BaseModel):
    """Private strict model for one unified rate-limit header pair."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    utilization: _Metadata
    reset: _Metadata


class _ClaudeHeaderReset(BaseModel):
    """Private strict model for one unified reset header."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    reset: _Metadata


class _ClaudeActivityModelUsage(BaseModel):
    """Private strict model for one cached model aggregate."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    input_tokens: int = Field(alias="inputTokens", ge=0, le=_MAX_TOKEN_COUNT)
    output_tokens: int = Field(
        alias="outputTokens",
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )
    cache_read_input_tokens: int | None = Field(
        default=None,
        alias="cacheReadInputTokens",
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )
    cache_creation_input_tokens: int | None = Field(
        default=None,
        alias="cacheCreationInputTokens",
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )


class _ClaudeActivityCache(BaseModel):
    """Private strict model for Claude's historical activity cache."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    model_usage: dict[str, _ClaudeActivityModelUsage] = Field(
        alias="modelUsage"
    )
    last_computed_date: _Metadata = Field(alias="lastComputedDate")
    first_session_date: _Metadata | None = Field(
        default=None,
        alias="firstSessionDate",
    )


class _ClaudeAssistantUsage(BaseModel):
    """Private strict model for one assistant token record."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    input_tokens: int = Field(ge=0, le=_MAX_TOKEN_COUNT)
    output_tokens: int = Field(ge=0, le=_MAX_TOKEN_COUNT)
    cache_read_input_tokens: int | None = Field(
        default=None,
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )
    cache_creation_input_tokens: int | None = Field(
        default=None,
        ge=0,
        le=_MAX_TOKEN_COUNT,
    )


class _ClaudeAssistantMessage(BaseModel):
    """Private strict model for the relevant assistant message fields."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    usage: _ClaudeAssistantUsage


class _ClaudeAssistantRecord(BaseModel):
    """Private strict model for one transcript assistant event."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    kind: str = Field(alias="type", pattern="^assistant$")
    timestamp: _Metadata
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    message: _ClaudeAssistantMessage


_USAGE_ADAPTER = TypeAdapter(_ClaudeUsageResponse)
_USAGE_WINDOW_ADAPTER = TypeAdapter(_ClaudeUsageWindow)
_HEADER_WINDOW_ADAPTER = TypeAdapter(_ClaudeHeaderWindow)
_HEADER_RESET_ADAPTER = TypeAdapter(_ClaudeHeaderReset)
_HEADERS_ADAPTER = TypeAdapter(
    dict[str, str],
    config=ConfigDict(strict=True),
)
_ACTIVITY_CACHE_ADAPTER = TypeAdapter(_ClaudeActivityCache)
_ASSISTANT_RECORD_ADAPTER = TypeAdapter(_ClaudeAssistantRecord)


@dataclass(frozen=True, slots=True)
class ClaudeActivityCache:
    """Validated historical total and inclusive live-scan boundary."""

    total_tokens: int
    last_computed_date: date
    first_session_date: date | None


@dataclass(frozen=True, slots=True)
class ClaudeActivityEvent:
    """Validated non-cached usage from one assistant transcript event."""

    occurred_at: datetime
    total_tokens: int
    is_sidechain: bool


def claude_failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    cause: ProviderFailureCause | None = None,
    action_required: bool = True,
    fields: tuple[str, ...] = (),
) -> ProviderFailure:
    """Build one secret-safe Claude provider failure."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        message=message,
        cause=cause,
        action_required=action_required,
        fields=fields,
    )


def _safe_paths(error: ValidationError) -> tuple[str, ...]:
    details = error.errors(include_input=False, include_url=False)
    if not details:
        return ("payload",)
    result: list[str] = []
    for detail in details:
        path = tuple(
            segment
            for segment in detail["loc"]
            if isinstance(segment, int) or segment in _SAFE_PATH_SEGMENTS
        )
        rendered = (
            ".".join(str(segment) for segment in path) if path else "payload"
        )
        if rendered not in result:
            result.append(rendered)
    return tuple(result)


def _validation_kind(error: ValidationError) -> ProviderFailureKind:
    details = error.errors(include_input=False, include_url=False)
    if any(detail["type"] == "missing" for detail in details):
        return ProviderFailureKind.INCOMPLETE
    return ProviderFailureKind.MALFORMED


def _validate[T](
    adapter: TypeAdapter[T],
    value: object,
    *,
    boundary: str,
) -> T:
    try:
        result = adapter.validate_python(value, strict=True)
    except ValidationError as validation_error:
        kind = _validation_kind(validation_error)
        adjective = (
            "incomplete"
            if kind is ProviderFailureKind.INCOMPLETE
            else "invalid"
        )
        fields = _safe_paths(validation_error)
        error = ProviderBoundaryError(
            claude_failure(
                kind,
                f"Claude {boundary} data is {adjective} at "
                f"{', '.join(fields)}.",
                fields=fields,
            )
        )
    else:
        return result
    raise error from None


def _activity_error(message: str) -> ProviderBoundaryError:
    return ProviderBoundaryError(
        claude_failure(
            ProviderFailureKind.MALFORMED,
            message,
            action_required=False,
        )
    )


def _activity_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _activity_error(
            "Claude activity data contains an invalid timestamp."
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _activity_error(
            "Claude activity data contains an invalid timestamp."
        )
    return parsed.astimezone(UTC)


def parse_activity_cache(value: JsonObject) -> ClaudeActivityCache:
    """Validate and aggregate Claude's historical activity cache."""
    validated = _validate(
        _ACTIVITY_CACHE_ADAPTER,
        value,
        boundary="activity cache",
    )
    try:
        boundary = date.fromisoformat(validated.last_computed_date)
    except ValueError:
        raise _activity_error(
            "Claude activity cache has an invalid computation date."
        ) from None
    first_session = (
        None
        if validated.first_session_date is None
        else _activity_time(validated.first_session_date).date()
    )
    total = sum(
        usage.input_tokens + usage.output_tokens
        for usage in validated.model_usage.values()
    )
    if total > _MAX_TOKEN_COUNT:
        raise _activity_error("Claude activity total exceeds its boundary.")
    return ClaudeActivityCache(total, boundary, first_session)


def parse_activity_record(
    value: JsonObject,
) -> ClaudeActivityEvent | None:
    """Return one validated assistant activity event, when relevant."""
    if value.get("type") != "assistant":
        return None
    validated = _validate(
        _ASSISTANT_RECORD_ADAPTER,
        value,
        boundary="activity transcript",
    )
    total = (
        validated.message.usage.input_tokens
        + validated.message.usage.output_tokens
    )
    if total > _MAX_TOKEN_COUNT:
        raise _activity_error("Claude activity event exceeds its boundary.")
    return ClaudeActivityEvent(
        occurred_at=_activity_time(validated.timestamp),
        total_tokens=total,
        is_sidechain=validated.is_sidechain,
    )


def provider_time(value: JsonValue | None) -> datetime | None:
    """Normalize one optional Claude response timestamp."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude usage data has an invalid reset timestamp.",
            )
        ) from None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.MALFORMED,
                "Claude usage data has an invalid reset timestamp.",
            )
        ) from None
    return parsed.astimezone(UTC)


def oauth_usage_window(
    value: JsonValue | None,
    label: str,
) -> UsageWindow | None:
    """Validate and normalize one optional OAuth usage bucket."""
    if value is None:
        return None
    validated = _validate(
        _USAGE_WINDOW_ADAPTER,
        value,
        boundary="usage",
    )
    return UsageWindow(
        name=label,
        utilization=float(validated.utilization),
        resets_at=provider_time(validated.resets_at),
    )


def oauth_usage_windows(
    value: JsonObject,
) -> tuple[UsageWindow, ...]:
    """Validate and normalize all requested OAuth usage buckets."""
    validated = _validate(_USAGE_ADAPTER, value, boundary="usage")
    windows_by_key = {
        "five_hour": validated.five_hour,
        "seven_day": validated.seven_day,
        "seven_day_opus": validated.seven_day_opus,
        "seven_day_oauth_apps": validated.seven_day_oauth_apps,
    }
    result: list[UsageWindow] = []
    for key, label in _OAUTH_USAGE_BUCKETS:
        window = windows_by_key[key]
        if window is not None:
            result.append(
                UsageWindow(
                    name=label,
                    utilization=float(window.utilization),
                    resets_at=provider_time(window.resets_at),
                )
            )
    return tuple(result)


def header_usage_window(
    prefix: str,
    label: str,
    response_headers: dict[str, str],
) -> UsageWindow | None:
    """Validate one unified rate-limit header pair when present."""
    headers = _validate(
        _HEADERS_ADAPTER,
        response_headers,
        boundary="header",
    )
    util_raw = headers.get(f"{prefix}-utilization")
    reset_raw = headers.get(f"{prefix}-reset")
    if util_raw is None and reset_raw is None:
        return None
    if util_raw is None or reset_raw is None:
        raise ProviderBoundaryError(
            claude_failure(
                ProviderFailureKind.INCOMPLETE,
                "Claude rate-limit headers are incomplete.",
            )
        ) from None
    validated = _validate(
        _HEADER_WINDOW_ADAPTER,
        {"utilization": util_raw, "reset": reset_raw},
        boundary="header",
    )
    utilization = _header_decimal(validated.utilization)
    if not Decimal(0) <= utilization <= Decimal(1):
        raise _invalid_header()
    reset_unix = _header_epoch(validated.reset)
    return UsageWindow(
        name=label,
        utilization=float(utilization * 100),
        resets_at=_from_epoch(reset_unix),
    )


def header_reset(
    response_headers: dict[str, str],
    prefix: str,
) -> datetime | None:
    """Validate one unified Claude rate-limit reset header."""
    headers = _validate(
        _HEADERS_ADAPTER,
        response_headers,
        boundary="header",
    )
    reset_raw = headers.get(f"{prefix}-reset")
    if reset_raw is None:
        return None
    validated = _validate(
        _HEADER_RESET_ADAPTER,
        {"reset": reset_raw},
        boundary="header",
    )
    return _from_epoch(_header_epoch(validated.reset))


def _header_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise _invalid_header() from None
    if not parsed.is_finite():
        raise _invalid_header()
    return parsed


def _header_epoch(value: str) -> int:
    parsed = _header_decimal(value)
    if parsed != parsed.to_integral_value() or parsed < 0:
        raise _invalid_header()
    return int(parsed)


def _from_epoch(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OSError, OverflowError, ValueError:
        raise _invalid_header() from None


def _invalid_header() -> ProviderBoundaryError:
    return ProviderBoundaryError(
        claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude rate-limit headers contain invalid numeric data.",
        )
    )
