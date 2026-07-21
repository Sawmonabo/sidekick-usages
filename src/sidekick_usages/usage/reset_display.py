"""Pure human reset-countdown formatting for usage presentation."""

from datetime import datetime

from rich.text import Text

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def reset_text(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Render a reset timestamp with local and relative time."""
    if reset_at is None:
        return Text("—", style="dim")
    seconds = int((reset_at - reference_time).total_seconds())
    if seconds <= 0:
        relative = "any moment"
    elif seconds < _SECONDS_PER_HOUR:
        relative = f"in {seconds // 60}m"
    elif seconds < _SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // 60, 60)
        relative = f"in {hours}h {minutes}m"
    else:
        days, remainder = divmod(seconds, _SECONDS_PER_DAY)
        relative = f"in {days}d {remainder // _SECONDS_PER_HOUR}h"
    local = reset_at.astimezone()
    return Text(
        f"↻ {local.strftime('%a %b %d, %I:%M %p')} ({relative})",
        style="dim",
    )


def compact_reset_text(
    reset_at: datetime | None,
    reference_time: datetime,
) -> str:
    """Render a reset timestamp as one compact relative countdown."""
    if reset_at is None:
        return ""
    seconds = round((reset_at - reference_time).total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // 60}m"
    if seconds < _SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes}m"
    days, remainder = divmod(seconds, _SECONDS_PER_DAY)
    return f"{days}d {remainder // _SECONDS_PER_HOUR}h"


__all__ = ["compact_reset_text", "reset_text"]
