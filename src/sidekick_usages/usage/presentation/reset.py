"""Human reset-countdown formatting for usage presentation."""

from datetime import datetime

from rich.text import Text

from sidekick_usages.branding.rich import rich_style
from sidekick_usages.usage.presentation.formatting import format_reset_text
from sidekick_usages.usage.presentation.theme import HEADER_STYLE


def reset_text(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Render a reset timestamp with local and relative time."""
    return Text(
        format_reset_text(reset_at, reference_time),
        style=rich_style(HEADER_STYLE),
    )
