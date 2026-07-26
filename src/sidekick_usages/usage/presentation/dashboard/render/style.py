"""ANSI adapter for semantically styled dashboard text."""

from collections.abc import Mapping

from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
    DashboardTextStyle,
)

ANSI_RESET = "\x1b[0m"
ANSI_DIM = "\x1b[2m"
ANSI_ROBOT = "\x1b[38;5;247m"
ANSI_TITLE = "\x1b[1;38;5;253m"
ANSI_PRODUCT = "\x1b[1;38;5;247m"
ANSI_DESCRIPTION = "\x1b[38;5;251m"
ANSI_PROMISE = "\x1b[38;5;247m"
ANSI_CLAUDE = "\x1b[35m"
ANSI_CODEX = "\x1b[36m"
ANSI_CLAUDE_TITLE = "\x1b[1;35m"
ANSI_CODEX_TITLE = "\x1b[1;36m"
ANSI_HEADER = "\x1b[38;5;242m"
ANSI_LABEL = "\x1b[1;38;5;253m"
ANSI_PLAN_MAX = "\x1b[35m"
ANSI_PLAN_TEAM = "\x1b[36m"
ANSI_PLAN_GREEN = "\x1b[32m"
ANSI_PLAN_YELLOW = "\x1b[33m"
ANSI_PLAN_DIM = "\x1b[2m"
ANSI_CURSOR = "\x1b[1;36m"
ANSI_WARNING = "\x1b[33m"
ANSI_RESET_TEXT = "\x1b[38;5;242m"
ANSI_HEAT_RED = "\x1b[97;41m"
ANSI_HEAT_YELLOW = "\x1b[97;43m"
ANSI_HEAT_CYAN = "\x1b[97;46m"
ANSI_HEAT_GREEN = "\x1b[97;42m"
ANSI_HEAT_ZERO = "\x1b[37;100m"
ANSI_FOOTER_KEYS = "\x1b[38;5;240m"
ANSI_FOOTER_HELP = "\x1b[38;5;250m"
ANSI_FOOTER_PROGRESS = "\x1b[36m"
ANSI_FOOTER_CONFIRMATION = "\x1b[33m"
ANSI_FOOTER_ERROR = "\x1b[31m"
ANSI_BY_STYLE = {
    DashboardTextStyle.DIM: ANSI_DIM,
    DashboardTextStyle.ROBOT: ANSI_ROBOT,
    DashboardTextStyle.TITLE: ANSI_TITLE,
    DashboardTextStyle.PRODUCT: ANSI_PRODUCT,
    DashboardTextStyle.DESCRIPTION: ANSI_DESCRIPTION,
    DashboardTextStyle.PROMISE: ANSI_PROMISE,
    DashboardTextStyle.CLAUDE: ANSI_CLAUDE,
    DashboardTextStyle.CODEX: ANSI_CODEX,
    DashboardTextStyle.CLAUDE_TITLE: ANSI_CLAUDE_TITLE,
    DashboardTextStyle.CODEX_TITLE: ANSI_CODEX_TITLE,
    DashboardTextStyle.HEADER: ANSI_HEADER,
    DashboardTextStyle.LABEL: ANSI_LABEL,
    DashboardTextStyle.PLAN_MAX: ANSI_PLAN_MAX,
    DashboardTextStyle.PLAN_TEAM: ANSI_PLAN_TEAM,
    DashboardTextStyle.PLAN_GREEN: ANSI_PLAN_GREEN,
    DashboardTextStyle.PLAN_YELLOW: ANSI_PLAN_YELLOW,
    DashboardTextStyle.PLAN_DIM: ANSI_PLAN_DIM,
    DashboardTextStyle.CURSOR: ANSI_CURSOR,
    DashboardTextStyle.WARNING: ANSI_WARNING,
    DashboardTextStyle.RESET: ANSI_RESET_TEXT,
    DashboardTextStyle.HEAT_ZERO: ANSI_HEAT_ZERO,
    DashboardTextStyle.HEAT_GREEN: ANSI_HEAT_GREEN,
    DashboardTextStyle.HEAT_CYAN: ANSI_HEAT_CYAN,
    DashboardTextStyle.HEAT_YELLOW: ANSI_HEAT_YELLOW,
    DashboardTextStyle.HEAT_RED: ANSI_HEAT_RED,
    DashboardTextStyle.FOOTER_KEYS: ANSI_FOOTER_KEYS,
    DashboardTextStyle.FOOTER_HELP: ANSI_FOOTER_HELP,
    DashboardTextStyle.FOOTER_PROGRESS: ANSI_FOOTER_PROGRESS,
    DashboardTextStyle.FOOTER_CONFIRMATION: ANSI_FOOTER_CONFIRMATION,
    DashboardTextStyle.FOOTER_ERROR: ANSI_FOOTER_ERROR,
}


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
        code = ANSI_BY_STYLE.get(segment.style)
        if code is None:
            rendered.append(segment.value)
        else:
            rendered.append(f"{code}{segment.value}{ANSI_RESET}")
    return "".join(rendered)
