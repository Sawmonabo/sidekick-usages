"""Dependency-free canonical product theme."""

from sidekick_usages.branding.models import BrandTextRole, TerminalStyle
from sidekick_usages.core.types import ProviderId

CLAUDE_COLOR = "magenta"
CODEX_COLOR = "cyan"
PROVIDER_COLORS: dict[ProviderId, str] = {
    ProviderId.CLAUDE: CLAUDE_COLOR,
    ProviderId.CODEX: CODEX_COLOR,
}
PLAIN_STYLE = TerminalStyle()
ROBOT_STYLE = TerminalStyle(foreground="#9e9e9e")
TITLE_STYLE = TerminalStyle(foreground="#dadada", bold=True)
PRODUCT_STYLE = TerminalStyle(foreground="#9e9e9e", bold=True)
DESCRIPTION_STYLE = TerminalStyle(foreground="#c6c6c6")
PROMISE_STYLE = TerminalStyle(foreground="#9e9e9e")
CLAUDE_STYLE = TerminalStyle(foreground=CLAUDE_COLOR)
CODEX_STYLE = TerminalStyle(foreground=CODEX_COLOR)
SECTION_STYLE = TerminalStyle(foreground="#b2b2b2", bold=True)
DIVIDER_STYLE = TerminalStyle(foreground="#3a3a3a")
UPDATE_SEPARATOR_STYLE = TerminalStyle(foreground="#6c6c6c")
UPDATE_LABEL_STYLE = TerminalStyle(foreground="#9e9e9e")
BRAND_STYLES: dict[BrandTextRole, TerminalStyle] = {
    BrandTextRole.PLAIN: PLAIN_STYLE,
    BrandTextRole.ROBOT: ROBOT_STYLE,
    BrandTextRole.TITLE: TITLE_STYLE,
    BrandTextRole.PRODUCT: PRODUCT_STYLE,
    BrandTextRole.DESCRIPTION: DESCRIPTION_STYLE,
    BrandTextRole.PROMISE: PROMISE_STYLE,
    BrandTextRole.CLAUDE: CLAUDE_STYLE,
    BrandTextRole.CODEX: CODEX_STYLE,
    BrandTextRole.DIVIDER: DIVIDER_STYLE,
}
