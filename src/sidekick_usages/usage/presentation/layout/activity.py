"""Token-activity summary layout without lookup-result dependencies."""

from datetime import date

from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import ProviderId

TOKENS_PER_THOUSAND = 1_000
TOKENS_PER_MILLION = 1_000_000
TOKENS_PER_BILLION = 1_000_000_000


def format_tokens_exact(value: int) -> str:
    """Render an exact token count with grouped digits."""
    return f"{value:,}"


def format_tokens_compact(value: int) -> str:
    """Render a compact token count without hiding useful precision."""
    if value >= TOKENS_PER_BILLION:
        amount = f"{value / TOKENS_PER_BILLION:.3f}"
        suffix = "B"
    elif value >= TOKENS_PER_MILLION:
        amount = f"{value / TOKENS_PER_MILLION:.2f}"
        suffix = "M"
    elif value >= TOKENS_PER_THOUSAND:
        amount = f"{value / TOKENS_PER_THOUSAND:.2f}"
        suffix = "K"
    else:
        return str(value)
    return f"{amount.rstrip('0').rstrip('.')}{suffix}"


def _format_since(value: date) -> str:
    """Render a source date as ``Mon D, YYYY``."""
    return f"{value:%b} {value.day}, {value.year}"


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
            f"  ·  since {_format_since(summary.since)}",
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
                f"since {_format_since(summary.since)}",
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
