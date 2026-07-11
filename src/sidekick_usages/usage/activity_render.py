"""Rich text for scoped provider token activity."""

from datetime import date
from typing import assert_never

from rich.text import Text

from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.usage.models import (
    CompleteTokenActivity,
    FailedTokenActivity,
    PartialTokenActivity,
    ProviderTokenActivity,
    TokenActivityFailureKind,
    UnavailableTokenActivity,
)

_TOKENS_PER_THOUSAND = 1_000
_TOKENS_PER_MILLION = 1_000_000
_TOKENS_PER_BILLION = 1_000_000_000


def format_tokens_exact(value: int) -> str:
    """Render an exact token count with grouped digits."""
    return f"{value:,}"


def format_tokens_compact(value: int) -> str:
    """Render a compact token count without hiding useful precision."""
    if value >= _TOKENS_PER_BILLION:
        amount = f"{value / _TOKENS_PER_BILLION:.3f}"
        suffix = "B"
    elif value >= _TOKENS_PER_MILLION:
        amount = f"{value / _TOKENS_PER_MILLION:.2f}"
        suffix = "M"
    elif value >= _TOKENS_PER_THOUSAND:
        amount = f"{value / _TOKENS_PER_THOUSAND:.2f}"
        suffix = "K"
    else:
        return str(value)
    return f"{amount.rstrip('0').rstrip('.')}{suffix}"


def _format_since(value: date) -> str:
    """Render a source date as ``Mon D``."""
    return f"{value:%b} {value.day}"


def activity_failure_label(kind: TokenActivityFailureKind) -> str:
    """Map one typed activity issue to concise presentation copy."""
    match kind:
        case TokenActivityFailureKind.SOURCE_UNREADABLE:
            return "token activity source unreadable"
        case TokenActivityFailureKind.SOURCE_MALFORMED:
            return "token activity source malformed"
        case TokenActivityFailureKind.AUTHENTICATION:
            return "token activity authentication failed"
        case TokenActivityFailureKind.FORBIDDEN:
            return "token activity forbidden"
        case TokenActivityFailureKind.RATE_LIMITED:
            return "token activity rate limited"
        case (
            TokenActivityFailureKind.TRANSIENT
            | TokenActivityFailureKind.PROVIDER
        ):
            return "token activity temporarily unavailable"
        case _ as unreachable:
            assert_never(unreachable)


def activity_text(
    activity: ProviderTokenActivity,
    *,
    compact: bool,
) -> Text:
    """Render one completed token-activity outcome without source I/O."""
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        formatter = format_tokens_compact if compact else format_tokens_exact
        qualifier = (
            " known tokens"
            if isinstance(activity, PartialTokenActivity)
            else " tokens"
        )
        rendered = Text(
            f"{formatter(activity.summary.total_tokens)}{qualifier}",
            style="grey54",
        )
        if activity.summary.scope is TokenActivityScope.LOCAL_INSTALLATION:
            rendered.append(
                "  ·  local" if compact else "  ·  local CLI",
                style="grey42",
            )
        if not compact and activity.summary.since is not None:
            rendered.append(
                f"  ·  since {_format_since(activity.summary.since)} ",
                style="grey35",
            )
        return rendered
    if isinstance(activity, UnavailableTokenActivity):
        return Text("token activity unavailable", style="grey42")
    if isinstance(activity, FailedTokenActivity):
        return Text(
            activity_failure_label(activity.issues[0].kind),
            style="yellow",
        )
    assert_never(activity)


__all__ = [
    "activity_failure_label",
    "activity_text",
    "format_tokens_compact",
    "format_tokens_exact",
]
