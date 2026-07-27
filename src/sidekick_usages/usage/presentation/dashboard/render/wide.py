"""Wide dashboard panels and semantic provider tables."""

from datetime import datetime

from sidekick_usages.core.models import (
    TokenActivitySummary,
    UsageWindow,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardExternalRow,
    DashboardProvider,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardLine,
)
from sidekick_usages.usage.presentation.dashboard.render.text import (
    activity_summary_line,
    brand_lines,
    concat_lines,
    fit_line,
    line,
    line_width,
    plain_line,
    segment,
    visible_plan,
    wrap_text,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    CURSOR_GLYPH,
    provider_detail,
    row_detail,
    row_is_selected,
    row_label,
    row_plan,
)
from sidekick_usages.usage.presentation.formatting import (
    PANEL_CHROME_WIDTH,
    PANEL_TILE_WIDTH,
    compact_reset_text,
    panel_columns,
    panel_model_width,
    sanitize_terminal_text,
    window_index,
)
from sidekick_usages.usage.presentation.theme import (
    UsageTextRole,
    heat_role,
    plan_role,
    provider_role,
    provider_title_role,
)

PANEL_PADDING = 2
TABLE_CELL_GAP = "  "


def dashboard_required_width(
    providers: tuple[DashboardProvider, ...],
    name_width: int,
    activities: dict[ProviderId, TokenActivitySummary],
) -> int:
    """Return the widest natural provider panel."""
    return max(
        (
            _panel_required_width(
                provider,
                name_width,
                activities.get(provider.provider_id),
            )
            for provider in providers
        ),
        default=1,
    )


def render_wide(
    snapshot: DashboardSnapshot,
    cursor: DashboardCursor,
    activities: dict[ProviderId, TokenActivitySummary],
    name_width: int,
    width: int,
    rendered_footer: list[DashboardLine],
) -> list[DashboardLine]:
    """Render one fixed-width panel layout."""
    lines = [DashboardLine(), *brand_lines(width), DashboardLine()]
    for provider in snapshot.providers:
        if not provider.rows:
            continue
        lines.extend(
            _provider_panel_lines(
                provider,
                cursor,
                name_width,
                activities.get(provider.provider_id),
                snapshot.reference_time,
                width,
            )
        )
        lines.append(DashboardLine())
    lines.extend(
        (_legend(), DashboardLine(), *rendered_footer, DashboardLine())
    )
    return lines


def _provider_panel_lines(
    provider: DashboardProvider,
    cursor: DashboardCursor,
    name_width: int,
    activity: TokenActivitySummary | None,
    reference_time: datetime,
    width: int,
) -> list[DashboardLine]:
    title = _provider_title(provider)
    top_fill = width - line_width(title) - 5
    provider_color_role = provider_role(provider.provider_id)
    top = concat_lines(
        plain_line("╭─ ", provider_color_role),
        title,
        plain_line(f" {'─' * top_fill}╮", provider_color_role),
    )
    content_width = width - PANEL_CHROME_WIDTH
    content = _provider_content_lines(
        provider,
        cursor,
        name_width,
        reference_time,
        content_width,
    )
    lines = [
        top,
        _panel_line(DashboardLine(), content_width, provider_color_role),
    ]
    lines.extend(
        _panel_line(line, content_width, provider_color_role)
        for line in content
    )
    lines.append(
        _panel_line(DashboardLine(), content_width, provider_color_role)
    )
    lines.append(_panel_bottom(width, activity, provider_color_role))
    return lines


def _provider_content_lines(
    provider: DashboardProvider,
    cursor: DashboardCursor,
    name_width: int,
    reference_time: datetime,
    content_width: int,
) -> list[DashboardLine]:
    reports = [
        row.usage.report
        for row in provider.rows
        if isinstance(row, DashboardAccount) and row.usage is not None
    ]
    primary, grouped = panel_columns(reports)
    widths, alignments = _table_layout(name_width, primary, grouped)
    lines = _table_header_lines(primary, grouped, widths, alignments)
    detail = provider_detail(provider)
    if detail is not None:
        lines.extend(_advisory_lines(detail, widths, content_width))
        lines.append(DashboardLine())
    for position, row in enumerate(provider.rows):
        if position:
            lines.append(DashboardLine())
        lines.extend(
            _account_table_lines(
                row,
                cursor,
                primary,
                grouped,
                widths,
                alignments,
                reference_time,
            )
        )
        detail = row_detail(
            row,
            cursor,
            reference_time,
            actions_enabled=provider.actions_enabled,
        )
        if detail is not None:
            lines.extend(
                _advisory_lines(
                    detail,
                    widths,
                    content_width,
                )
            )
    return lines


def _table_header_lines(
    primary: list[str],
    grouped: list[tuple[str, list[str]]],
    widths: list[int],
    alignments: list[str],
) -> list[DashboardLine]:
    lines: list[DashboardLine] = []
    if grouped:
        caption = [
            DashboardLine(),
            DashboardLine(),
            DashboardLine(),
            *([DashboardLine()] * len(primary)),
        ]
        for group, _lengths in grouped:
            caption.extend(
                (
                    DashboardLine(),
                    plain_line(group, UsageTextRole.MODEL_CAPTION),
                )
            )
        lines.extend(
            (
                _join_cells(caption, widths, alignments),
                _join_cells(
                    [DashboardLine()] * len(widths),
                    widths,
                    alignments,
                ),
            )
        )
    header = [
        DashboardLine(),
        DashboardLine(),
        DashboardLine(),
        *(plain_line(length, UsageTextRole.HEADER) for length in primary),
    ]
    for _group, lengths in grouped:
        header.extend(
            (
                plain_line("│", UsageTextRole.MODEL_RULE),
                _subgrid(
                    [
                        plain_line(length, UsageTextRole.HEADER)
                        for length in lengths
                    ]
                ),
            )
        )
    lines.append(_join_cells(header, widths, alignments))
    return lines


def _account_table_lines(
    row: DashboardAccount | DashboardExternalRow,
    cursor: DashboardCursor,
    primary: list[str],
    grouped: list[tuple[str, list[str]]],
    widths: list[int],
    alignments: list[str],
    reference_time: datetime,
) -> list[DashboardLine]:
    report = (
        row.usage.report
        if isinstance(row, DashboardAccount) and row.usage is not None
        else None
    )
    windows = {} if report is None else window_index(report)
    safe_plan = visible_plan(row_plan(row))
    usage_cells = [
        _marker(row, cursor),
        plain_line(
            sanitize_terminal_text(row_label(row)),
            UsageTextRole.ACCOUNT_LABEL,
        ),
        plain_line(safe_plan, plan_role(safe_plan)),
    ]
    usage_cells.extend(
        _utilization_tile(windows.get(("", length))) for length in primary
    )
    for group, lengths in grouped:
        usage_cells.extend(
            (
                plain_line("│", UsageTextRole.MODEL_RULE),
                _subgrid(
                    [
                        _utilization_tile(windows.get((group, length)))
                        for length in lengths
                    ]
                ),
            )
        )
    lines = [_join_cells(usage_cells, widths, alignments)]
    if report is not None:
        lines.append(
            _reset_row(
                windows,
                primary,
                grouped,
                widths,
                alignments,
                reference_time,
            )
        )
    return lines


def _marker(
    row: DashboardAccount | DashboardExternalRow,
    cursor: DashboardCursor,
) -> DashboardLine:
    selected = row_is_selected(row, cursor)
    return line(
        segment(
            CURSOR_GLYPH if selected else " ",
            (UsageTextRole.CURSOR if selected else UsageTextRole.PLAIN),
        ),
        segment(" "),
        segment("●", provider_role(row.provider_id)),
    )


def _reset_row(
    windows: dict[tuple[str, str], UsageWindow],
    primary: list[str],
    grouped: list[tuple[str, list[str]]],
    widths: list[int],
    alignments: list[str],
    reference_time: datetime,
) -> DashboardLine:
    cells = [DashboardLine(), DashboardLine(), DashboardLine()]
    cells.extend(
        _reset_tile(windows.get(("", length)), reference_time)
        for length in primary
    )
    for group, lengths in grouped:
        cells.extend(
            (
                plain_line("│", UsageTextRole.MODEL_RULE),
                _subgrid(
                    [
                        _reset_tile(
                            windows.get((group, length)),
                            reference_time,
                        )
                        for length in lengths
                    ]
                ),
            )
        )
    return _join_cells(cells, widths, alignments)


def _advisory_lines(
    detail: str,
    widths: list[int],
    content_width: int,
) -> list[DashboardLine]:
    prefix_width = widths[0] + len(TABLE_CELL_GAP)
    prefix = " " * prefix_width
    return wrap_text(
        f"⚠ {sanitize_terminal_text(detail)}",
        content_width,
        UsageTextRole.ADVISORY,
        initial_prefix=prefix,
        subsequent_prefix=prefix,
    )


def _table_layout(
    name_width: int,
    primary: list[str],
    grouped: list[tuple[str, list[str]]],
) -> tuple[list[int], list[str]]:
    widths = [
        3,
        name_width,
        4,
        *([PANEL_TILE_WIDTH] * len(primary)),
    ]
    alignments = ["left", "left", "left", *(["center"] * len(primary))]
    for group, lengths in grouped:
        widths.extend((1, panel_model_width(group, len(lengths))))
        alignments.extend(("center", "left"))
    return widths, alignments


def _join_cells(
    cells: list[DashboardLine],
    widths: list[int],
    alignments: list[str],
) -> DashboardLine:
    if len(cells) != len(widths):
        raise ValueError("Dashboard table row has the wrong cell count.")
    parts: list[DashboardLine] = []
    for position, (cell, width, alignment) in enumerate(
        zip(cells, widths, alignments, strict=True)
    ):
        if position:
            parts.append(plain_line(TABLE_CELL_GAP))
        parts.append(fit_line(cell, width, alignment))
    rendered = concat_lines(*parts)
    return _rstrip_line(rendered)


def _rstrip_line(rendered: DashboardLine) -> DashboardLine:
    parts = list(rendered.segments)
    while parts and parts[-1].style is UsageTextRole.PLAIN:
        item = parts[-1]
        value = item.value.rstrip()
        if value:
            parts[-1] = segment(value)
            break
        parts.pop()
    return DashboardLine(tuple(parts))


def _subgrid(cells: list[DashboardLine]) -> DashboardLine:
    parts: list[DashboardLine] = []
    for position, cell in enumerate(cells):
        if position:
            parts.append(plain_line(TABLE_CELL_GAP))
        parts.append(fit_line(cell, PANEL_TILE_WIDTH, "center"))
    return concat_lines(*parts)


def _utilization_tile(window: UsageWindow | None) -> DashboardLine:
    if window is None:
        return DashboardLine()
    percent = round(window.utilization)
    value = f"{percent}%".center(PANEL_TILE_WIDTH)
    return plain_line(value, heat_role(percent))


def _reset_tile(
    window: UsageWindow | None,
    reference_time: datetime,
) -> DashboardLine:
    if window is None:
        return DashboardLine()
    return plain_line(
        compact_reset_text(window.resets_at, reference_time),
        UsageTextRole.RESET,
    )


def _panel_line(
    content: DashboardLine,
    content_width: int,
    provider_role: UsageTextRole,
) -> DashboardLine:
    return concat_lines(
        plain_line("│", provider_role),
        plain_line(" " * PANEL_PADDING),
        fit_line(content, content_width, "left"),
        plain_line(" " * PANEL_PADDING),
        plain_line("│", provider_role),
    )


def _panel_bottom(
    width: int,
    activity: TokenActivitySummary | None,
    provider_role: UsageTextRole,
) -> DashboardLine:
    if activity is None:
        return plain_line(f"╰{'─' * (width - 2)}╯", provider_role)
    subtitle = activity_summary_line(activity, compact=False)
    fill = width - line_width(subtitle) - 5
    return concat_lines(
        plain_line(f"╰{'─' * fill} ", provider_role),
        subtitle,
        plain_line(" ─╯", provider_role),
    )


def _panel_required_width(
    provider: DashboardProvider,
    name_width: int,
    activity: TokenActivitySummary | None,
) -> int:
    reports = [
        row.usage.report
        for row in provider.rows
        if isinstance(row, DashboardAccount) and row.usage is not None
    ]
    primary, grouped = panel_columns(reports)
    widths, _alignments = _table_layout(name_width, primary, grouped)
    table_width = sum(widths) + len(TABLE_CELL_GAP) * (len(widths) - 1)
    title_width = line_width(_provider_title(provider))
    subtitle_width = (
        0
        if activity is None
        else line_width(activity_summary_line(activity, compact=False))
    )
    return max(
        table_width + PANEL_CHROME_WIDTH,
        title_width + PANEL_CHROME_WIDTH,
        subtitle_width + PANEL_CHROME_WIDTH,
    )


def _provider_title(provider: DashboardProvider) -> DashboardLine:
    count = sum(isinstance(row, DashboardAccount) for row in provider.rows)
    noun = "account" if count == 1 else "accounts"
    return line(
        segment(
            provider.provider_id.upper(),
            provider_title_role(provider.provider_id),
        ),
        segment(
            f" · {count} {noun}",
            UsageTextRole.PANEL_META,
        ),
    )


def _legend() -> DashboardLine:
    parts: list[DashboardLine] = []
    for label, value in (
        ("<40", 20),
        ("40-69", 55),
        ("70-89", 80),
        ("≥90", 95),
    ):
        parts.extend(
            (
                plain_line(f" {label} ", heat_role(value)),
                plain_line("  "),
            )
        )
    parts.append(plain_line("   dim = resets in", UsageTextRole.LEGEND))
    return concat_lines(*parts)
