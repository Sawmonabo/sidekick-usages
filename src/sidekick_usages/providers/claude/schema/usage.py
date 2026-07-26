"""Strict Claude usage schemas and normalization."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
)

from sidekick_usages.core.models import UsageWindow
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.errors import claude_failure
from sidekick_usages.providers.claude.schema.validation import (
    bounded_string,
    validate_payload,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue

_MAX_METADATA_BYTES = 4_096
_MAX_UTILIZATION_PERCENT = 100
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
_OAUTH_USAGE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_oauth_apps", "7d OAuth"),
)


type _Metadata = Annotated[str, AfterValidator(_metadata)]
type _Utilization = Annotated[
    int | float,
    AfterValidator(_utilization),
]


def _metadata(value: str) -> str:
    return bounded_string(value, _MAX_METADATA_BYTES)


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


@cache
def _usage_adapter() -> TypeAdapter[_ClaudeUsageResponse]:
    return TypeAdapter(_ClaudeUsageResponse)


@cache
def _usage_window_adapter() -> TypeAdapter[_ClaudeUsageWindow]:
    return TypeAdapter(_ClaudeUsageWindow)


@cache
def _header_window_adapter() -> TypeAdapter[_ClaudeHeaderWindow]:
    return TypeAdapter(_ClaudeHeaderWindow)


@cache
def _header_reset_adapter() -> TypeAdapter[_ClaudeHeaderReset]:
    return TypeAdapter(_ClaudeHeaderReset)


@cache
def _headers_adapter() -> TypeAdapter[dict[str, str]]:
    return TypeAdapter(
        dict[str, str],
        config=ConfigDict(strict=True),
    )


@cache
def _activity_cache_adapter() -> TypeAdapter[_ClaudeActivityCache]:
    return TypeAdapter(_ClaudeActivityCache)


@cache
def _assistant_record_adapter() -> TypeAdapter[_ClaudeAssistantRecord]:
    return TypeAdapter(_ClaudeAssistantRecord)


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
    validated = validate_payload(
        _activity_cache_adapter(),
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
    validated = validate_payload(
        _assistant_record_adapter(),
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
    validated = validate_payload(
        _usage_window_adapter(),
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
    validated = validate_payload(
        _usage_adapter(),
        value,
        boundary="usage",
    )
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
    headers = validate_payload(
        _headers_adapter(),
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
    validated = validate_payload(
        _header_window_adapter(),
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
    headers = validate_payload(
        _headers_adapter(),
        response_headers,
        boundary="header",
    )
    reset_raw = headers.get(f"{prefix}-reset")
    if reset_raw is None:
        return None
    validated = validate_payload(
        _header_reset_adapter(),
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
