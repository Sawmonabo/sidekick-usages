"""Claude payload parsing and provider-native time conversion."""

from datetime import UTC, datetime, timedelta

from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    ClaudeCredentials,
    DetectedCredentials,
    UsageWindow,
)
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization import JsonObject, JsonValue


def parse_credentials_blob(
    blob: JsonObject,
) -> DetectedCredentials | None:
    """Parse one Claude Code credential object without coercion."""
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    raw_scopes = oauth.get("scopes")
    scopes: tuple[str, ...] | None
    if isinstance(raw_scopes, list) and all(
        isinstance(scope, str) for scope in raw_scopes
    ):
        scopes = tuple(scope for scope in raw_scopes if isinstance(scope, str))
    else:
        scopes = None
    refresh = oauth.get("refreshToken")
    plan = oauth.get("subscriptionType")
    return DetectedCredentials(
        credentials=ClaudeCredentials(
            access_token=token,
            refresh_token=refresh if isinstance(refresh, str) else None,
            expiry=claude_expiry(oauth.get("expiresAt")),
            scopes=scopes,
        ),
        plan=plan if isinstance(plan, str) and plan else "unknown",
    )


def claude_expiry(value: JsonValue | None) -> Expiry:
    """Normalize Claude's native millisecond expiry metadata."""
    if value is None:
        return UnknownExpiry()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return InvalidExpiry()
    try:
        return KnownExpiry(
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
        )
    except OverflowError:
        return InvalidExpiry()


def refresh_expiry(value: JsonValue, reference_time: datetime) -> Expiry:
    """Normalize Claude refresh-relative expiry metadata."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidPayloadError
    normalized = reference_time.astimezone(UTC)
    normalized = normalized.replace(
        microsecond=(normalized.microsecond // 1000) * 1000
    )
    return KnownExpiry(normalized + timedelta(seconds=value))


def provider_time(value: JsonValue | None) -> datetime | None:
    """Normalize one optional Claude response timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def oauth_usage_window(
    value: JsonValue | None,
    label: str,
) -> UsageWindow | None:
    """Parse one OAuth usage bucket when present."""
    if not isinstance(value, dict) or not value:
        return None
    utilization_value = value.get("utilization")
    utilization = (
        float(utilization_value)
        if isinstance(utilization_value, int | float | str)
        else 0.0
    )
    return UsageWindow(
        name=label,
        utilization=utilization,
        resets_at=provider_time(value.get("resets_at")),
    )


def header_usage_window(
    prefix: str,
    label: str,
    response_headers: dict[str, str],
) -> UsageWindow | None:
    """Parse one unified rate-limit header pair when numeric."""
    util_raw = response_headers.get(f"{prefix}-utilization")
    reset_raw = response_headers.get(f"{prefix}-reset")
    if util_raw is None or reset_raw is None:
        return None
    try:
        utilization = float(util_raw) * 100
        reset_unix = int(float(reset_raw))
    except TypeError, ValueError:
        return None
    return UsageWindow(
        name=label,
        utilization=utilization,
        resets_at=datetime.fromtimestamp(reset_unix, tz=UTC),
    )


def header_reset(
    response_headers: dict[str, str],
    prefix: str,
) -> datetime | None:
    """Parse one unified Claude rate-limit reset header."""
    reset_raw = response_headers.get(f"{prefix}-reset")
    if reset_raw is None:
        return None
    try:
        reset_unix = int(float(reset_raw))
    except TypeError, ValueError:
        return None
    return datetime.fromtimestamp(reset_unix, tz=UTC)
