"""Rich presentation for scoped provider token activity."""

from typing import assert_never

from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.usage.models import (
    CompleteTokenActivity,
    FailedTokenActivity,
    PartialTokenActivity,
    ProviderTokenActivity,
    TokenActivityFailureKind,
    UnavailableTokenActivity,
)
from sidekick_usages.usage.presentation.layout.activity import (
    narrow_activity_summary,
    panel_activity_summary,
)
from sidekick_usages.usage.presentation.theme import (
    ADVISORY_STYLE,
    HEADER_STYLE,
)


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


def _state_text(
    activity: UnavailableTokenActivity | FailedTokenActivity,
) -> Text:
    if isinstance(activity, UnavailableTokenActivity):
        return Text(
            "token activity unavailable",
            style=rich_style(HEADER_STYLE),
        )
    if isinstance(activity, FailedTokenActivity):
        return Text(
            activity_failure_label(activity.issues[0].kind),
            style=rich_style(ADVISORY_STYLE),
        )
    assert_never(activity)


def panel_activity_text(activity: ProviderTokenActivity) -> Text:
    """Render the exact one-line activity label for a framed panel."""
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        return panel_activity_summary(activity.summary)
    return _state_text(activity)


def narrow_activity_lines(
    activity: ProviderTokenActivity,
) -> tuple[Text, ...]:
    """Render compact activity as deliberate narrow-terminal lines."""
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        return narrow_activity_summary(activity.summary)
    return (_state_text(activity),)
