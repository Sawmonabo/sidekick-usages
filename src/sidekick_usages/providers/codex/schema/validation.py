"""Shared strict validation for Codex provider payload schemas."""

from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.auth.token import validated_codex_token
from sidekick_usages.providers.codex.failures import codex_failure

MAX_METADATA_BYTES = 4_096
MAX_PLAN_BYTES = 256
MAX_TIMESTAMP_BYTES = 4_096
SAFE_PATH_SEGMENTS = frozenset(
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

type TokenString = Annotated[
    str,
    AfterValidator(validated_codex_token),
]
type MetadataString = Annotated[str, AfterValidator(_metadata)]
type PlanString = Annotated[str, AfterValidator(_plan)]
type OpaqueTimestamp = Annotated[str, AfterValidator(_timestamp)]
type Utilization = Annotated[
    int | float,
    Field(ge=0, le=100, allow_inf_nan=False),
]
type Epoch = Annotated[int | float, Field(ge=0, allow_inf_nan=False)]


def _bounded_utf8(value: str, maximum: int) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError from error
    if not encoded or len(encoded) > maximum:
        raise ValueError
    return value


def _metadata(value: str) -> str:
    return _bounded_utf8(value, MAX_METADATA_BYTES)


def _plan(value: str) -> str:
    return _bounded_utf8(value, MAX_PLAN_BYTES)


def _timestamp(value: str) -> str:
    return _bounded_utf8(value, MAX_TIMESTAMP_BYTES)


class ProviderSchema(BaseModel):
    """Strict provider model that intentionally tolerates new fields."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


def validate_provider_payload[T](
    adapter: TypeAdapter[T],
    value: object,
    *,
    message: str,
) -> T:
    """Validate one strict payload or raise a redacted provider failure."""
    try:
        return adapter.validate_python(value, strict=True)
    except ValidationError as error:
        fields = _safe_paths(error)
        raise ProviderBoundaryError(
            codex_failure(
                _validation_kind(error),
                message,
                fields=fields,
            )
        ) from None


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
    if segment in SAFE_PATH_SEGMENTS:
        return segment
    return None
