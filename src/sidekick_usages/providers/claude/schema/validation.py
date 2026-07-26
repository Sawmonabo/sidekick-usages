"""Shared validation for untrusted Claude provider payloads."""

from pydantic import TypeAdapter, ValidationError

from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.errors import claude_failure

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
        "organizationUuid",
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
        "timestamp",
        "tokenAccount",
        "type",
        "usage",
        "utilization",
    }
)


def bounded_string(value: str, maximum: int) -> str:
    """Require bounded, nonempty UTF-8 provider text."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError from error
    if not encoded or len(encoded) > maximum:
        raise ValueError
    return value


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


def validate_payload[T](
    adapter: TypeAdapter[T],
    value: object,
    *,
    boundary: str,
) -> T:
    """Validate one Claude payload and expose only safe field paths."""
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
