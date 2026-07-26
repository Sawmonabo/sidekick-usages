"""Rich-free product identity shared by every terminal surface."""

from sidekick_usages.branding.models import (
    BrandLine,
    BrandText,
    BrandTextRole,
)

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
FULL_BRAND_LAYOUT = (
    BrandLine((BrandText(ROBOT_LINES[0], BrandTextRole.ROBOT),)),
    BrandLine((BrandText(ROBOT_LINES[1], BrandTextRole.ROBOT),)),
    BrandLine(
        (
            BrandText(ROBOT_LINES[2], BrandTextRole.ROBOT),
            BrandText("    ", BrandTextRole.ROBOT),
            BrandText(BRAND_NAME, BrandTextRole.TITLE),
            BrandText(f" {BRAND_PRODUCT}", BrandTextRole.PRODUCT),
        )
    ),
    BrandLine(
        (
            BrandText(ROBOT_LINES[3][:4], BrandTextRole.ROBOT),
            BrandText(ROBOT_LINES[3][4], BrandTextRole.CLAUDE),
            BrandText(ROBOT_LINES[3][5:8], BrandTextRole.ROBOT),
            BrandText(ROBOT_LINES[3][8], BrandTextRole.CODEX),
            BrandText(ROBOT_LINES[3][9:], BrandTextRole.ROBOT),
            BrandText("   ", BrandTextRole.ROBOT),
            BrandText(">", BrandTextRole.CLAUDE),
            BrandText(">", BrandTextRole.CODEX),
            BrandText(f" {BRAND_DESCRIPTION}", BrandTextRole.DESCRIPTION),
        )
    ),
    BrandLine(
        (
            BrandText(ROBOT_LINES[4], BrandTextRole.ROBOT),
            BrandText("   ", BrandTextRole.ROBOT),
            BrandText(">", BrandTextRole.CLAUDE),
            BrandText(">", BrandTextRole.CODEX),
            BrandText(f" {BRAND_PROMISE}", BrandTextRole.PROMISE),
        )
    ),
    BrandLine((BrandText(ROBOT_LINES[5], BrandTextRole.ROBOT),)),
)
NARROW_BRAND_LAYOUT = (
    BrandLine((BrandText(ROBOT_LINES[0], BrandTextRole.ROBOT),)),
    BrandLine((BrandText(ROBOT_LINES[1], BrandTextRole.ROBOT),)),
    BrandLine(
        (
            BrandText(ROBOT_LINES[2], BrandTextRole.ROBOT),
            BrandText("  ", BrandTextRole.ROBOT),
            BrandText(BRAND_NAME, BrandTextRole.TITLE),
            BrandText(f" {BRAND_PRODUCT}", BrandTextRole.PRODUCT),
        )
    ),
    BrandLine(
        (
            BrandText(ROBOT_LINES[3][:4], BrandTextRole.ROBOT),
            BrandText(ROBOT_LINES[3][4], BrandTextRole.CLAUDE),
            BrandText(ROBOT_LINES[3][5:8], BrandTextRole.ROBOT),
            BrandText(ROBOT_LINES[3][8], BrandTextRole.CODEX),
            BrandText(ROBOT_LINES[3][9:], BrandTextRole.ROBOT),
        )
    ),
    BrandLine((BrandText(ROBOT_LINES[4], BrandTextRole.ROBOT),)),
    BrandLine((BrandText(ROBOT_LINES[5], BrandTextRole.ROBOT),)),
)
MINIMAL_BRAND_LAYOUT = BrandLine(
    (
        BrandText(BRAND_NAME, BrandTextRole.TITLE),
        BrandText(f" {BRAND_PRODUCT}", BrandTextRole.PRODUCT),
    )
)
FULL_HEADER_MIN_WIDTH = max(len(line.plain) for line in FULL_BRAND_LAYOUT)
NARROW_HEADER_MIN_WIDTH = max(len(line.plain) for line in NARROW_BRAND_LAYOUT)


def brand_layout(width: int) -> tuple[BrandLine, ...]:
    """Return one canonical responsive semantic masthead layout."""
    safe_width = max(1, width)
    if safe_width >= FULL_HEADER_MIN_WIDTH:
        rows = FULL_BRAND_LAYOUT
    elif safe_width >= NARROW_HEADER_MIN_WIDTH:
        rows = NARROW_BRAND_LAYOUT
    else:
        rows = (_clip_line(MINIMAL_BRAND_LAYOUT, safe_width),)
    divider = BrandLine((BrandText("─" * safe_width, BrandTextRole.DIVIDER),))
    return (*rows, divider)


def _clip_line(line: BrandLine, width: int) -> BrandLine:
    remaining = width
    segments: list[BrandText] = []
    for segment in line.segments:
        if remaining <= 0:
            break
        value = segment.value[:remaining]
        if value:
            segments.append(BrandText(value, segment.role))
        remaining -= len(value)
    return BrandLine(tuple(segments))
