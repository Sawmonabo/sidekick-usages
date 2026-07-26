"""Strict Codex usage schemas and provider-native time conversion."""

from datetime import UTC, datetime
from functools import cache

from pydantic import Field, TypeAdapter

from sidekick_usages.core.models import UsageReport, UsageWindow
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.failures import codex_failure
from sidekick_usages.providers.codex.schema.validation import (
    Epoch,
    MetadataString,
    PlanString,
    ProviderSchema,
    Utilization,
    validate_provider_payload,
)
from sidekick_usages.serialization.json import JsonObject


class UsageWindowSchema(ProviderSchema):
    """One Codex usage window."""

    used_percent: Utilization
    resets_at: MetadataString | None = None
    reset_at: Epoch | None = None


class RateLimitSchema(ProviderSchema):
    """Primary and secondary Codex usage windows."""

    primary_window: UsageWindowSchema | None = None
    secondary_window: UsageWindowSchema | None = None


class AdditionalRateLimitSchema(ProviderSchema):
    """One named additional Codex usage limit."""

    limit_name: MetadataString | None = None
    label: MetadataString | None = None
    model: MetadataString | None = None
    rate_limit: RateLimitSchema | None = None
    used_percent: Utilization | None = None
    resets_at: MetadataString | None = None
    reset_at: Epoch | None = None


class UsageResponseSchema(ProviderSchema):
    """Supported Codex usage response fields."""

    plan_type: PlanString | None = None
    plan: PlanString | None = None
    rate_limit: RateLimitSchema | None = None
    primary_window: UsageWindowSchema | None = None
    secondary_window: UsageWindowSchema | None = None
    additional_rate_limits: list[AdditionalRateLimitSchema] = Field(
        default_factory=list
    )


@cache
def _usage_adapter() -> TypeAdapter[UsageResponseSchema]:
    return TypeAdapter(UsageResponseSchema)


def parse_usage_response(value: JsonObject) -> UsageReport:
    """Validate and normalize one Codex usage response."""
    response = validate_provider_payload(
        _usage_adapter(),
        value,
        message="Codex returned an invalid usage response.",
    )
    rate_limit = response.rate_limit or RateLimitSchema(
        primary_window=response.primary_window,
        secondary_window=response.secondary_window,
    )
    windows = _rate_limit_windows(rate_limit)
    for extra in response.additional_rate_limits:
        label = extra.limit_name or extra.label or extra.model
        if label is None:
            raise ProviderBoundaryError(
                codex_failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Codex returned an unnamed additional usage limit.",
                )
            )
        if extra.rate_limit is not None:
            windows.extend(_rate_limit_windows(extra.rate_limit, label))
            continue
        if extra.used_percent is None:
            raise ProviderBoundaryError(
                codex_failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Codex returned an incomplete additional usage limit.",
                )
            )
        windows.append(
            _usage_window(
                label,
                UsageWindowSchema(
                    used_percent=extra.used_percent,
                    resets_at=extra.resets_at,
                    reset_at=extra.reset_at,
                ),
            )
        )
    if not windows:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.INCOMPLETE,
                "Codex returned no supported usage windows.",
            )
        )
    return UsageReport(
        windows=tuple(windows),
        plan=response.plan_type or response.plan,
    )


def _rate_limit_windows(
    rate_limit: RateLimitSchema,
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


def _usage_window(name: str, window: UsageWindowSchema) -> UsageWindow:
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
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned a timezone-free usage reset timestamp.",
            )
        )
    return parsed.astimezone(UTC)


def _epoch_time(value: int | float) -> datetime:
    if isinstance(value, bool):
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OverflowError, OSError, ValueError:
        raise ProviderBoundaryError(
            codex_failure(
                ProviderFailureKind.MALFORMED,
                "Codex returned an invalid usage reset timestamp.",
            )
        ) from None
