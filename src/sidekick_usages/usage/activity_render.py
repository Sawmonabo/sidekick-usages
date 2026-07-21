"""Rich text for scoped provider token activity."""

from datetime import date
from typing import assert_never

from rich.text import Text

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
    """Render a source date as ``Mon D, YYYY``."""
    return f"{value:%b} {value.day}, {value.year}"


def activity_failure_label(kind: TokenActivityFailureKind) -> str:
    """Map one typed activity issue to concise presentation copy."""
    match kind:
        case TokenActivityFailureKind.SOURCE_UNREADABLE:
            label = "token activity source unreadable"
        case TokenActivityFailureKind.SOURCE_MALFORMED:
            label = "token activity source malformed"
        case TokenActivityFailureKind.AUTHENTICATION:
            label = "token activity authentication failed"
        case TokenActivityFailureKind.FORBIDDEN:
            label = "token activity forbidden"
        case TokenActivityFailureKind.RATE_LIMITED:
            label = "token activity rate limited"
        case (
            TokenActivityFailureKind.TRANSIENT
            | TokenActivityFailureKind.PROVIDER
        ):
            label = "token activity temporarily unavailable"
        case TokenActivityFailureKind.PERSISTENCE:
            label = "saved token activity unavailable"
        case _ as unreachable:
            assert_never(unreachable)
    return label


def _summary_text(
    activity: CompleteTokenActivity | PartialTokenActivity,
    *,
    compact: bool,
) -> Text:
    formatter = format_tokens_compact if compact else format_tokens_exact
    return Text(
        f"{formatter(activity.summary.total_tokens)} tokens",
        style="grey54",
    )


def _state_text(
    activity: UnavailableTokenActivity | FailedTokenActivity,
) -> Text:
    if isinstance(activity, UnavailableTokenActivity):
        return Text("token activity unavailable", style="grey42")
    if isinstance(activity, FailedTokenActivity):
        return Text(
            activity_failure_label(activity.issues[0].kind),
            style="yellow",
        )
    assert_never(activity)


def panel_activity_text(activity: ProviderTokenActivity) -> Text:
    """Render the exact one-line activity label for a framed panel."""
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        rendered = _summary_text(activity, compact=False)
        if activity.summary.since is not None:
            rendered.append(
                f"  ·  since {_format_since(activity.summary.since)}",
                style="grey35",
            )
        return rendered
    return _state_text(activity)


def narrow_activity_lines(
    activity: ProviderTokenActivity,
) -> tuple[Text, ...]:
    """Render compact activity as deliberate narrow-terminal lines."""
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        lines = [_summary_text(activity, compact=True)]
        if activity.summary.since is not None:
            lines.append(
                Text(
                    f"since {_format_since(activity.summary.since)}",
                    style="grey35",
                )
            )
        return tuple(lines)
    return (_state_text(activity),)


__all__ = [
    "activity_failure_label",
    "format_tokens_compact",
    "format_tokens_exact",
    "narrow_activity_lines",
    "panel_activity_text",
]
