"""Token-activity summary layout without lookup-result dependencies."""

from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.formatting import (
    format_since,
    format_tokens_compact,
    format_tokens_exact,
)
from sidekick_usages.usage.presentation.theme import (
    ACTIVITY_SINCE_STYLE,
    PANEL_META_STYLE,
    provider_title_role,
    usage_style,
)


def _summary_text(summary: TokenActivitySummary, *, compact: bool) -> Text:
    formatter = format_tokens_compact if compact else format_tokens_exact
    return Text(
        f"{formatter(summary.total_tokens)} tokens",
        style=rich_style(PANEL_META_STYLE),
    )


def panel_activity_summary(summary: TokenActivitySummary) -> Text:
    """Render one exact activity summary for a framed panel."""
    rendered = _summary_text(summary, compact=False)
    if summary.since is not None:
        rendered.append(
            f"  ·  since {format_since(summary.since)}",
            style=rich_style(ACTIVITY_SINCE_STYLE),
        )
    return rendered


def narrow_activity_summary(
    summary: TokenActivitySummary,
) -> tuple[Text, ...]:
    """Render one compact activity summary as narrow-terminal lines."""
    lines = [_summary_text(summary, compact=True)]
    if summary.since is not None:
        lines.append(
            Text(
                f"since {format_since(summary.since)}",
                style=rich_style(ACTIVITY_SINCE_STYLE),
            )
        )
    return tuple(lines)


def provider_activity_lines(
    provider_id: ProviderId,
    activity_lines: tuple[Text, ...],
) -> tuple[Text, ...]:
    """Prefix narrow activity lines with one provider heading."""
    provider_name = provider_id.upper()
    prefix_width = len(provider_name) + len(" · ")
    rendered: list[Text] = []
    for position, activity_line in enumerate(activity_lines):
        line = Text()
        if position == 0:
            line.append(
                provider_name,
                style=rich_style(
                    usage_style(provider_title_role(provider_id))
                ),
            )
            line.append(" · ", style=rich_style(PANEL_META_STYLE))
        else:
            line.append(" " * prefix_width)
        line.append_text(activity_line)
        rendered.append(line)
    return tuple(rendered)
