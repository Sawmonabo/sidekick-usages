"""Human reset-countdown formatting for usage presentation."""

from datetime import datetime

from rich.text import Text

from sidekick_usages.usage.presentation.formatting import format_reset_text


def reset_text(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Render a reset timestamp with local and relative time."""
    return Text(format_reset_text(reset_at, reference_time), style="dim")
