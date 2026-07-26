"""Rich adapters for non-dashboard product surfaces."""

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding.content import (
    BRAND_NAME,
    BRAND_PRODUCT,
    brand_layout,
)
from sidekick_usages.branding.models import (
    BrandLine,
    BrandTextRole,
)
from sidekick_usages.core.types import ProviderId

PROVIDER_COLORS: dict[str, str] = {
    ProviderId.CLAUDE: "magenta",
    ProviderId.CODEX: "cyan",
}
ROBOT_STYLE = "grey62"
TITLE_STYLE = "bold grey85"
PRODUCT_STYLE = "bold grey62"
DESCRIPTION_STYLE = "grey78"
PROMISE_STYLE = "grey62"
SECTION_STYLE = "bold grey70"
DIVIDER_STYLE = "grey23"
BRAND_STYLES = {
    BrandTextRole.PLAIN: None,
    BrandTextRole.ROBOT: ROBOT_STYLE,
    BrandTextRole.TITLE: TITLE_STYLE,
    BrandTextRole.PRODUCT: PRODUCT_STYLE,
    BrandTextRole.DESCRIPTION: DESCRIPTION_STYLE,
    BrandTextRole.PROMISE: PROMISE_STYLE,
    BrandTextRole.CLAUDE: PROVIDER_COLORS[ProviderId.CLAUDE],
    BrandTextRole.CODEX: PROVIDER_COLORS[ProviderId.CODEX],
    BrandTextRole.DIVIDER: DIVIDER_STYLE,
}


def _render_line(line: BrandLine) -> Text:
    """Adapt one canonical masthead line to Rich text."""
    rendered = Text()
    for segment in line.segments:
        rendered.append(segment.value, style=BRAND_STYLES[segment.role])
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
                Text(section, style=SECTION_STYLE),
                Text(""),
            )
        )
    return Group(*parts)


def update_status_line() -> Text:
    """Render compact update-status branding plus a matching divider."""
    line = Text()
    line.append(BRAND_NAME, style=TITLE_STYLE)
    line.append(f" {BRAND_PRODUCT}", style=PRODUCT_STYLE)
    line.append(" · ", style="grey42")
    line.append("update status", style="grey62")
    divider = "─" * line.cell_len
    line.append(f"\n{divider}", style=DIVIDER_STYLE)
    return line
