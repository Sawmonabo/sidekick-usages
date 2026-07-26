"""Token-activity summary layout without lookup-result dependencies."""

from rich.text import Text

from sidekick_usages.branding.rich import PROVIDER_COLORS
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.presentation.formatting import (
    format_since,
    format_tokens_compact,
    format_tokens_exact,
)


def _summary_text(summary: TokenActivitySummary, *, compact: bool) -> Text:
    formatter = format_tokens_compact if compact else format_tokens_exact
    return Text(
        f"{formatter(summary.total_tokens)} tokens",
        style="grey54",
    )


def panel_activity_summary(summary: TokenActivitySummary) -> Text:
    """Render one exact activity summary for a framed panel."""
    rendered = _summary_text(summary, compact=False)
    if summary.since is not None:
        rendered.append(
            f"  ·  since {format_since(summary.since)}",
            style="grey35",
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
                style="grey35",
            )
        )
    return tuple(lines)


def provider_activity_lines(
    provider_id: ProviderId,
    activity_lines: tuple[Text, ...],
) -> tuple[Text, ...]:
    """Prefix narrow activity lines with one provider heading."""
    provider_name = provider_id.upper()
    provider_color = PROVIDER_COLORS.get(provider_id, "white")
    prefix_width = len(provider_name) + len(" · ")
    rendered: list[Text] = []
    for position, activity_line in enumerate(activity_lines):
        line = Text()
        if position == 0:
            line.append(provider_name, style=f"bold {provider_color}")
            line.append(" · ", style="grey54")
        else:
            line.append(" " * prefix_width)
        line.append_text(activity_line)
        rendered.append(line)
    return tuple(rendered)
