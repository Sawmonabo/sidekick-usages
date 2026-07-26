"""Rich adapters for non-dashboard product surfaces."""

from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text

from sidekick_usages.branding.content import (
    BRAND_NAME,
    BRAND_PRODUCT,
    brand_layout,
)
from sidekick_usages.branding.models import (
    BrandLine,
    TerminalStyle,
)
from sidekick_usages.branding.theme import (
    BRAND_STYLES,
    DIVIDER_STYLE,
    PRODUCT_STYLE,
    SECTION_STYLE,
    TITLE_STYLE,
    UPDATE_LABEL_STYLE,
    UPDATE_SEPARATOR_STYLE,
)


def rich_style(theme: TerminalStyle) -> Style:
    """Adapt one dependency-free terminal style to Rich."""
    return Style(
        color=theme.foreground,
        bgcolor=theme.background,
        bold=theme.bold,
        dim=theme.dim,
    )


def _render_line(line: BrandLine) -> Text:
    """Adapt one canonical masthead line to Rich text."""
    rendered = Text()
    for segment in line.segments:
        rendered.append(
            segment.value,
            style=rich_style(BRAND_STYLES[segment.role]),
        )
    return rendered


def brand_header(
    width: int,
    *,
    section: str | None = None,
) -> RenderableType:
    """Render the responsive application masthead."""
    parts: list[RenderableType] = [
        *(_render_line(line) for line in brand_layout(width))
    ]
    if section:
        parts.extend(
            (
                Text(""),
                Text(section, style=rich_style(SECTION_STYLE)),
                Text(""),
            )
        )
    return Group(*parts)


def update_status_line() -> Text:
    """Render compact update-status branding plus a matching divider."""
    line = Text()
    line.append(BRAND_NAME, style=rich_style(TITLE_STYLE))
    line.append(f" {BRAND_PRODUCT}", style=rich_style(PRODUCT_STYLE))
    line.append(" · ", style=rich_style(UPDATE_SEPARATOR_STYLE))
    line.append("update status", style=rich_style(UPDATE_LABEL_STYLE))
    divider = "─" * line.cell_len
    line.append(f"\n{divider}", style=rich_style(DIVIDER_STYLE))
    return line
