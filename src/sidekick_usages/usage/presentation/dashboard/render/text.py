"""Semantic dashboard text construction and cell-safe layout."""

from typing import assert_never

from wcwidth import wcwidth

from sidekick_usages.branding.content import brand_layout
from sidekick_usages.branding.models import BrandTextRole
from sidekick_usages.core.models import TokenActivitySummary
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardFooterKind,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
    DashboardText,
    DashboardTextStyle,
)
from sidekick_usages.usage.presentation.formatting import (
    ACTIVE_PERCENT_THRESHOLD,
    CYAN_PERCENT_THRESHOLD,
    RED_PERCENT_THRESHOLD,
    YELLOW_PERCENT_THRESHOLD,
    cell_width,
    format_since,
    format_tokens_compact,
    format_tokens_exact,
    sanitize_terminal_text,
)

KEY_FOOTER = (
    " ↑/↓ or j/k move   Tab provider   Enter use   r refresh   ? help   q exit"
)
HELP_FOOTER = (
    " ↑/↓ or j/k move   Tab provider   Enter use   Esc cancel   "
    "r refresh   R refresh all   ? close help   q exit"
)
BRAND_STYLES = {
    BrandTextRole.PLAIN: DashboardTextStyle.PLAIN,
    BrandTextRole.ROBOT: DashboardTextStyle.ROBOT,
    BrandTextRole.TITLE: DashboardTextStyle.TITLE,
    BrandTextRole.PRODUCT: DashboardTextStyle.PRODUCT,
    BrandTextRole.DESCRIPTION: DashboardTextStyle.DESCRIPTION,
    BrandTextRole.PROMISE: DashboardTextStyle.PROMISE,
    BrandTextRole.CLAUDE: DashboardTextStyle.CLAUDE,
    BrandTextRole.CODEX: DashboardTextStyle.CODEX,
    BrandTextRole.DIVIDER: DashboardTextStyle.DIM,
}


def segment(
    value: str,
    style: DashboardTextStyle = DashboardTextStyle.PLAIN,
) -> DashboardText:
    """Create one sanitized semantic text segment."""
    return DashboardText(sanitize_terminal_text(value), style)


def line(*segments: DashboardText) -> DashboardLine:
    """Create one line while dropping inert empty segments."""
    return DashboardLine(tuple(item for item in segments if item.value))


def plain_line(
    value: str,
    style: DashboardTextStyle = DashboardTextStyle.PLAIN,
) -> DashboardLine:
    """Create one sanitized single-role line."""
    return line(segment(value, style))


def concat_lines(*lines: DashboardLine) -> DashboardLine:
    """Concatenate semantic lines without inserting text."""
    return DashboardLine(
        tuple(segment for rendered in lines for segment in rendered.segments)
    )


def line_width(rendered: DashboardLine) -> int:
    """Return one semantic line's visible terminal width."""
    return cell_width(rendered.plain)


def clip_line(rendered: DashboardLine, width: int) -> DashboardLine:
    """Clip one semantic line to a bounded cell width."""
    if width <= 0:
        return DashboardLine()
    if line_width(rendered) <= width:
        return rendered
    if width == 1:
        return plain_line("…", DashboardTextStyle.DIM)
    target = width - 1
    used = 0
    clipped: list[DashboardText] = []
    for item in rendered.segments:
        characters: list[str] = []
        for character in item.value:
            character_width = max(0, wcwidth(character))
            if used + character_width > target:
                break
            characters.append(character)
            used += character_width
        if characters:
            clipped.append(DashboardText("".join(characters), item.style))
        if used >= target or len(characters) != len(item.value):
            break
    clipped.append(segment("…", DashboardTextStyle.DIM))
    return DashboardLine(tuple(clipped))


def fit_line(
    rendered: DashboardLine,
    width: int,
    alignment: str,
) -> DashboardLine:
    """Clip and pad one semantic table cell."""
    clipped = clip_line(rendered, width)
    padding = max(0, width - line_width(clipped))
    if alignment == "center":
        left = padding // 2
        return concat_lines(
            plain_line(" " * left),
            clipped,
            plain_line(" " * (padding - left)),
        )
    return concat_lines(clipped, plain_line(" " * padding))


def wrap_text(
    value: str,
    width: int,
    style: DashboardTextStyle,
    *,
    initial_prefix: str = "",
    subsequent_prefix: str = "",
) -> list[DashboardLine]:
    """Wrap sanitized single-role text at terminal cell boundaries."""
    safe_value = sanitize_terminal_text(value)
    safe_initial = sanitize_terminal_text(initial_prefix)
    safe_subsequent = sanitize_terminal_text(subsequent_prefix)
    if cell_width(safe_initial) + cell_width(safe_value) <= width:
        return [plain_line(f"{safe_initial}{safe_value}", style)]
    available = max(1, width - cell_width(safe_initial))
    words = safe_value.split()
    if not words:
        return [plain_line(safe_initial, style)]
    lines: list[DashboardLine] = []
    current = ""
    prefix = safe_initial
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if cell_width(candidate) <= available:
            current = candidate
            continue
        if current:
            lines.append(plain_line(f"{prefix}{current}", style))
            prefix = safe_subsequent
            available = max(1, width - cell_width(prefix))
            current = word
        else:
            clipped_word = clip_line(plain_line(word, style), available)
            lines.append(concat_lines(plain_line(prefix, style), clipped_word))
            prefix = safe_subsequent
            available = max(1, width - cell_width(prefix))
            current = ""
    if current:
        lines.append(
            clip_line(
                plain_line(f"{prefix}{current}", style),
                width,
            )
        )
    return lines


def activity_summary_line(
    activity: TokenActivitySummary,
    *,
    compact: bool,
) -> DashboardLine:
    """Render one token total and optional start date."""
    formatter = format_tokens_compact if compact else format_tokens_exact
    parts = [
        segment(
            f"{formatter(activity.total_tokens)} tokens",
            DashboardTextStyle.DIM,
        )
    ]
    if activity.since is not None:
        parts.append(
            segment(
                f"  ·  since {format_since(activity.since)}",
                DashboardTextStyle.RESET,
            )
        )
    return line(*parts)


def footer_lines(
    footer: DashboardFooter,
    width: int,
) -> list[DashboardLine]:
    """Render and wrap one typed dashboard footer."""
    style = _footer_style(footer.kind)
    match footer.kind:
        case DashboardFooterKind.KEYS:
            value = KEY_FOOTER
            prefix = ""
        case DashboardFooterKind.HELP:
            value = HELP_FOOTER
            prefix = ""
        case (
            DashboardFooterKind.PROGRESS
            | DashboardFooterKind.CONFIRMATION
            | DashboardFooterKind.ERROR
        ):
            if footer.message is None:
                raise ValueError("Transient footer requires a message.")
            value = footer.message
            prefix = " "
        case _ as unreachable:
            assert_never(unreachable)
    return wrap_text(
        value,
        width,
        style,
        initial_prefix=prefix,
    )


def visible_plan(plan: str) -> str:
    """Sanitize a plan and suppress empty or unknown labels."""
    safe_plan = sanitize_terminal_text(plan)
    return "" if not safe_plan or safe_plan == "unknown" else safe_plan


def provider_style(provider_id: ProviderId) -> DashboardTextStyle:
    """Return one provider's semantic color role."""
    if provider_id is ProviderId.CLAUDE:
        return DashboardTextStyle.CLAUDE
    return DashboardTextStyle.CODEX


def provider_title_style(provider_id: ProviderId) -> DashboardTextStyle:
    """Return one provider title's bold semantic color role."""
    if provider_id is ProviderId.CLAUDE:
        return DashboardTextStyle.CLAUDE_TITLE
    return DashboardTextStyle.CODEX_TITLE


def plan_style(plan: str) -> DashboardTextStyle:
    """Return the canonical plan-chip role."""
    normalized = plan.casefold()
    if normalized == "max":
        return DashboardTextStyle.PLAN_MAX
    if normalized == "team":
        return DashboardTextStyle.PLAN_TEAM
    if normalized in {"pro", "plus"}:
        return DashboardTextStyle.PLAN_GREEN
    if normalized in {"enterprise", "business"}:
        return DashboardTextStyle.PLAN_YELLOW
    return DashboardTextStyle.PLAN_DIM


def heat_style(percent: int) -> DashboardTextStyle:
    """Return the semantic heat role for one usage value."""
    if percent >= RED_PERCENT_THRESHOLD:
        return DashboardTextStyle.HEAT_RED
    if percent >= YELLOW_PERCENT_THRESHOLD:
        return DashboardTextStyle.HEAT_YELLOW
    if percent >= CYAN_PERCENT_THRESHOLD:
        return DashboardTextStyle.HEAT_CYAN
    if percent >= ACTIVE_PERCENT_THRESHOLD:
        return DashboardTextStyle.HEAT_GREEN
    return DashboardTextStyle.HEAT_ZERO


def brand_lines(width: int) -> list[DashboardLine]:
    """Adapt the canonical masthead layout to dashboard text."""
    return [
        line(
            *(
                segment(item.value, BRAND_STYLES[item.role])
                for item in brand_line.segments
            )
        )
        for brand_line in brand_layout(width)
    ]


def _footer_style(kind: DashboardFooterKind) -> DashboardTextStyle:
    match kind:
        case DashboardFooterKind.KEYS:
            return DashboardTextStyle.FOOTER_KEYS
        case DashboardFooterKind.HELP:
            return DashboardTextStyle.FOOTER_HELP
        case DashboardFooterKind.PROGRESS:
            return DashboardTextStyle.FOOTER_PROGRESS
        case DashboardFooterKind.CONFIRMATION:
            return DashboardTextStyle.FOOTER_CONFIRMATION
        case DashboardFooterKind.ERROR:
            return DashboardTextStyle.FOOTER_ERROR
        case _ as unreachable:
            assert_never(unreachable)
