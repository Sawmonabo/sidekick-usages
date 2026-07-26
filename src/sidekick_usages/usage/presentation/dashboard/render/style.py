"""ANSI adapter for semantically styled dashboard text."""

from collections.abc import Mapping
from functools import cache

from sidekick_usages.branding.models import TerminalStyle
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
)
from sidekick_usages.usage.presentation.theme import (
    UsageTextRole,
    usage_style,
)

ANSI_RESET = "\x1b[0m"
HEX_COLOR_LENGTH = 7
ANSI_FOREGROUND_CODES: dict[str, int] = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}
ANSI_BACKGROUND_CODES: dict[str, int] = {
    "black": 40,
    "red": 41,
    "green": 42,
    "yellow": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
}


@cache
def ansi_style(role: UsageTextRole) -> str:
    """Return the ANSI SGR prefix for one canonical semantic role."""
    return _ansi_style(usage_style(role))


def _ansi_style(theme: TerminalStyle) -> str:
    codes: list[str] = []
    if theme.bold:
        codes.append("1")
    if theme.dim:
        codes.append("2")
    if theme.foreground is not None:
        codes.append(
            _color_code(
                theme.foreground,
                38,
                ANSI_FOREGROUND_CODES,
            )
        )
    if theme.background is not None:
        codes.append(
            _color_code(
                theme.background,
                48,
                ANSI_BACKGROUND_CODES,
            )
        )
    return "" if not codes else f"\x1b[{';'.join(codes)}m"


def _color_code(
    color: str,
    true_color_prefix: int,
    basic_codes: Mapping[str, int],
) -> str:
    if color.startswith("#") and len(color) == HEX_COLOR_LENGTH:
        red = int(color[1:3], 16)
        green = int(color[3:5], 16)
        blue = int(color[5:7], 16)
        return f"{true_color_prefix};2;{red};{green};{blue}"
    return str(basic_codes[color])


def dashboard_color_enabled(
    environment: Mapping[str, str],
    *,
    terminal: bool,
) -> bool:
    """Return whether one terminal dashboard should emit ANSI color."""
    return (
        terminal
        and "NO_COLOR" not in environment
        and environment.get("TERM", "").casefold() != "dumb"
    )


def render_dashboard_lines(
    lines: list[DashboardLine],
    *,
    color: bool,
) -> str:
    """Render semantic lines with optional ANSI styling."""
    return "\n".join(_render_line(line, color=color) for line in lines) + "\n"


def _render_line(line: DashboardLine, *, color: bool) -> str:
    if not color:
        return line.plain
    rendered: list[str] = []
    for segment in line.segments:
        code = ansi_style(segment.style)
        if not code:
            rendered.append(segment.value)
        else:
            rendered.append(f"{code}{segment.value}{ANSI_RESET}")
    return "".join(rendered)
