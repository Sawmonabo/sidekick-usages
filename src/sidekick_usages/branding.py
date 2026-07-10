"""Shared Rich branding for human-facing ``sidekick-usages`` screens.

This module is the single source of truth for the robot art, product copy,
brand colors, and responsive header layouts. It intentionally has no
dependencies on accounts, providers, storage, HTTP services, or CLI state so
help rendering can use it without loading credentials.
"""

from rich.console import Group, RenderableType
from rich.text import Text

#: Provider colors shared by the robot and provider-specific usage panels.
PROVIDER_COLORS: dict[str, str] = {
    "claude": "magenta",
    "codex": "cyan",
}

#: Canonical robot art. No other source module should define these rows.
ROBOT_LINES: tuple[str, ...] = (
    "      o",
    "     .-.",
    "  .--┴-┴--.",
    "  | O   O |",
    "  | ||||| |",
    "  '--___--'",
)

BRAND_NAME = "sidekick"
BRAND_PRODUCT = "usages"
BRAND_TITLE = f"{BRAND_NAME} {BRAND_PRODUCT}"
BRAND_DESCRIPTION = (
    "A multi-account usage dashboard for Claude Code and Codex CLI."
)
BRAND_PROMISE = "Limits + resets + account status, one terminal."

_ROBOT_STYLE = "grey62"
_TITLE_STYLE = "bold grey85"
_PRODUCT_STYLE = "bold grey62"
_DESCRIPTION_STYLE = "grey78"
_PROMISE_STYLE = "grey62"
_SECTION_STYLE = "bold grey70"
_DIVIDER_STYLE = "grey23"

_FULL_PLAIN_LINES = (
    ROBOT_LINES[0],
    ROBOT_LINES[1],
    f"{ROBOT_LINES[2]}    {BRAND_TITLE}",
    f"{ROBOT_LINES[3]}   >> {BRAND_DESCRIPTION}",
    f"{ROBOT_LINES[4]}   >> {BRAND_PROMISE}",
    ROBOT_LINES[5],
)
_NARROW_PLAIN_LINES = (
    ROBOT_LINES[0],
    ROBOT_LINES[1],
    f"{ROBOT_LINES[2]}  {BRAND_TITLE}",
    ROBOT_LINES[3],
    ROBOT_LINES[4],
    ROBOT_LINES[5],
)

#: Minimum width that fits the complete logo plus both product-copy lines.
FULL_HEADER_MIN_WIDTH = max(Text(line).cell_len for line in _FULL_PLAIN_LINES)

#: Minimum width that fits the complete robot plus its title without copy.
NARROW_HEADER_MIN_WIDTH = max(
    Text(line).cell_len for line in _NARROW_PLAIN_LINES
)


def _robot_rows() -> list[Text]:
    """Return fresh styled rows for the canonical robot.

    :return: Six independent Rich text rows. The left and right eyes carry
        Claude and Codex colors as decoration; the plain art is unchanged.
    """
    rows = [Text(line, style=_ROBOT_STYLE) for line in ROBOT_LINES]
    eye_row = rows[3]
    eye_row.stylize(PROVIDER_COLORS["claude"], 4, 5)
    eye_row.stylize(PROVIDER_COLORS["codex"], 8, 9)
    return rows


def _append_title(row: Text, *, gap: str) -> None:
    """Append the styled application title to one robot row.

    :param row: Robot row to extend.
    :param gap: Horizontal spacing before the title.
    """
    row.append(gap)
    row.append(BRAND_NAME, style=_TITLE_STYLE)
    row.append(f" {BRAND_PRODUCT}", style=_PRODUCT_STYLE)


def _append_speech(row: Text, message: str, *, style: str) -> None:
    """Append one provider-colored ``>>`` speech line.

    :param row: Robot row to extend.
    :param message: Product-copy sentence spoken by the robot.
    :param style: Rich style for the product-copy sentence.
    """
    row.append("   ")
    row.append(">", style=PROVIDER_COLORS["claude"])
    row.append(">", style=PROVIDER_COLORS["codex"])
    row.append(f" {message}", style=style)


def _full_rows() -> list[Text]:
    """Compose the complete robot, title, and product copy."""
    rows = _robot_rows()
    _append_title(rows[2], gap="    ")
    _append_speech(rows[3], BRAND_DESCRIPTION, style=_DESCRIPTION_STYLE)
    _append_speech(rows[4], BRAND_PROMISE, style=_PROMISE_STYLE)
    return rows


def _narrow_rows() -> list[Text]:
    """Compose the complete robot and title without wide product copy."""
    rows = _robot_rows()
    _append_title(rows[2], gap="  ")
    return rows


def _minimal_rows() -> list[Text]:
    """Compose a one-line title for terminals narrower than the robot."""
    title = Text()
    title.append(BRAND_NAME, style=_TITLE_STYLE)
    title.append(f" {BRAND_PRODUCT}", style=_PRODUCT_STYLE)
    return [title]


def brand_header(
    width: int,
    *,
    section: str | None = None,
) -> RenderableType:
    """Render the responsive application masthead.

    The complete robot and product copy render at 79 cells or wider. Narrower
    screens keep the full robot but omit speech; extremely narrow screens use
    the title only. A section label, when supplied, appears below the divider.

    :param width: Available terminal width in cells.
    :param section: Optional command-specific section label.
    :return: A Rich group ready for direct printing or composition.
    """
    safe_width = max(1, width)
    if safe_width >= FULL_HEADER_MIN_WIDTH:
        rows = _full_rows()
    elif safe_width >= NARROW_HEADER_MIN_WIDTH:
        rows = _narrow_rows()
    else:
        rows = _minimal_rows()

    parts: list[RenderableType] = [
        *rows,
        Text("─" * safe_width, style=_DIVIDER_STYLE),
    ]
    if section:
        parts.extend(
            (
                Text(""),
                Text(section, style=_SECTION_STYLE),
                Text(""),
            )
        )
    return Group(*parts)


def brand_line(section: str) -> Text:
    """Render compact one-line branding plus a matching divider.

    :param section: Short status-surface label, such as ``update status``.
    :return: A two-line Rich text value containing title and divider.
    """
    line = Text()
    line.append(BRAND_NAME, style=_TITLE_STYLE)
    line.append(f" {BRAND_PRODUCT}", style=_PRODUCT_STYLE)
    line.append(" · ", style="grey42")
    line.append(section, style="grey62")
    divider = "─" * line.cell_len
    line.append(f"\n{divider}", style=_DIVIDER_STYLE)
    return line
