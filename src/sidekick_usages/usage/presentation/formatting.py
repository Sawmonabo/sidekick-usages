"""Rich-free formatting shared by dashboard and command presentation."""

import re
from datetime import date, datetime

from wcwidth import wcwidth

from sidekick_usages.core.models import UsageReport, UsageWindow

SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400
TOKENS_PER_THOUSAND = 1_000
TOKENS_PER_MILLION = 1_000_000
TOKENS_PER_BILLION = 1_000_000_000
RED_PERCENT_THRESHOLD = 90
YELLOW_PERCENT_THRESHOLD = 70
CYAN_PERCENT_THRESHOLD = 40
ACTIVE_PERCENT_THRESHOLD = 1
NARROW_BAR_WIDTH = 18
PANEL_TILE_WIDTH = 6
PANEL_CHROME_WIDTH = 6
PANEL_CELL_GAP_WIDTH = 2
CONTROL_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
C0_END = 0x1F
DELETE_CHARACTER = 0x7F
C1_END = 0x9F
WINDOW_LENGTH_PATTERN = re.compile(r"\d+[hd]")


def sanitize_terminal_text(value: str) -> str:
    """Replace terminal control characters before layout or output."""
    return "".join(
        (
            CONTROL_REPLACEMENT
            if (
                ord(character) <= C0_END
                or DELETE_CHARACTER <= ord(character) <= C1_END
            )
            else character
        )
        for character in value
    )


def cell_width(value: str) -> int:
    """Return the terminal cells occupied by plain text."""
    return sum(max(0, wcwidth(character)) for character in value)


def panel_model_width(name: str, length_count: int) -> int:
    """Return one named model group's terminal-cell column width."""
    tiles = PANEL_TILE_WIDTH * length_count + PANEL_CELL_GAP_WIDTH * (
        length_count - 1
    )
    return max(cell_width(name), tiles)


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


def format_since(value: date) -> str:
    """Render a source date as ``Mon D, YYYY``."""
    return f"{value:%b} {value.day}, {value.year}"


def format_reset_text(
    reset_at: datetime | None,
    reference_time: datetime,
) -> str:
    """Render a reset timestamp with local and relative time."""
    if reset_at is None:
        return "—"
    seconds = int((reset_at - reference_time).total_seconds())
    if seconds <= 0:
        relative = "any moment"
    elif seconds < SECONDS_PER_HOUR:
        relative = f"in {seconds // 60}m"
    elif seconds < SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // 60, 60)
        relative = f"in {hours}h {minutes}m"
    else:
        days, remainder = divmod(seconds, SECONDS_PER_DAY)
        relative = f"in {days}d {remainder // SECONDS_PER_HOUR}h"
    local = reset_at.astimezone()
    return f"↻ {local.strftime('%a %b %d, %I:%M %p')} ({relative})"


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
    if seconds < SECONDS_PER_HOUR:
        return f"{seconds // 60}m"
    if seconds < SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes}m"
    days, remainder = divmod(seconds, SECONDS_PER_DAY)
    return f"{days}d {remainder // SECONDS_PER_HOUR}h"


def utilization_bar_segments(
    percent: float,
    width: int = NARROW_BAR_WIDTH,
) -> tuple[str, str]:
    """Return filled and idle segments for one bounded usage bar."""
    bounded = max(0.0, min(100.0, percent))
    filled = round(bounded / 100.0 * width)
    return ("⣿" * filled, "⣀" * (width - filled))


def classify_window(name: str) -> tuple[str, str]:
    """Split a window name into its detected length and optional group."""
    safe_name = sanitize_terminal_text(name)
    match = WINDOW_LENGTH_PATTERN.search(safe_name)
    if match is None:
        return (safe_name.strip(), "")
    length = match.group(0)
    group = (safe_name[: match.start()] + safe_name[match.end() :]).strip()
    return (length, group)


def window_length_hours(length: str) -> int:
    """Return an hour-based sort key for one length token."""
    match = WINDOW_LENGTH_PATTERN.fullmatch(length)
    if match is None:
        return 0
    value = int(length[:-1])
    return value * 24 if length[-1] == "d" else value


def panel_columns(
    reports: list[UsageReport],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Derive primary and named-group columns from usage reports."""
    primary_windows: dict[str, int] = {}
    grouped_windows: dict[str, dict[str, int]] = {}
    for report in reports:
        for window in report.windows:
            length, group = classify_window(window.name)
            hours = window_length_hours(length)
            if group == "":
                primary_windows[length] = hours
            else:
                grouped_windows.setdefault(group, {})[length] = hours
    primary = sorted(
        primary_windows,
        key=lambda length: primary_windows[length],
    )
    grouped = [
        (
            group,
            sorted(lengths, key=lambda length: lengths[length]),
        )
        for group, lengths in sorted(grouped_windows.items())
    ]
    return primary, grouped


def window_index(
    report: UsageReport,
) -> dict[tuple[str, str], UsageWindow]:
    """Map each group and length pair to its usage window."""
    index: dict[tuple[str, str], UsageWindow] = {}
    for window in report.windows:
        length, group = classify_window(window.name)
        index[(group, length)] = window
    return index
