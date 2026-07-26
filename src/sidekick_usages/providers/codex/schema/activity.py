"""Strict Codex token-activity schemas."""

from datetime import date
from functools import cache

from pydantic import Field, TypeAdapter

from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.failures import codex_failure
from sidekick_usages.providers.codex.schema.validation import (
    MetadataString,
    ProviderSchema,
    validate_provider_payload,
)
from sidekick_usages.serialization.json import JsonObject

MAX_TOKEN_COUNT = 9_223_372_036_854_775_807


class TokenUsageDailyBucketSchema(ProviderSchema):
    """One dated Codex token-activity bucket."""

    start_date: MetadataString
    tokens: int = Field(ge=0, le=MAX_TOKEN_COUNT)


class TokenUsageStatsSchema(ProviderSchema):
    """Supported Codex token-activity statistics."""

    lifetime_tokens: int = Field(ge=0, le=MAX_TOKEN_COUNT)
    peak_daily_tokens: int | None = Field(
        default=None,
        ge=0,
        le=MAX_TOKEN_COUNT,
    )
    longest_running_turn_sec: int | None = Field(default=None, ge=0)
    current_streak_days: int | None = Field(default=None, ge=0)
    longest_streak_days: int | None = Field(default=None, ge=0)
    daily_usage_buckets: list[TokenUsageDailyBucketSchema] | None = None


class TokenUsageProfileSchema(ProviderSchema):
    """One Codex token-activity profile."""

    stats: TokenUsageStatsSchema


@cache
def _token_usage_profile_adapter() -> TypeAdapter[TokenUsageProfileSchema]:
    return TypeAdapter(TokenUsageProfileSchema)


def parse_activity_response(value: JsonObject) -> TokenActivitySummary:
    """Validate and normalize one Codex account activity profile."""
    profile = validate_provider_payload(
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
                codex_failure(
                    ProviderFailureKind.MALFORMED,
                    "Codex token activity contains an invalid bucket date.",
                    fields=("stats.daily_usage_buckets.start_date",),
                )
            ) from None
        if bucket_date in seen_dates:
            raise ProviderBoundaryError(
                codex_failure(
                    ProviderFailureKind.MALFORMED,
                    "Codex token activity contains duplicate bucket dates.",
                    fields=("stats.daily_usage_buckets.start_date",),
                )
            ) from None
        seen_dates.add(bucket_date)
        if bucket.tokens > MAX_TOKEN_COUNT - bucket_total:
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
