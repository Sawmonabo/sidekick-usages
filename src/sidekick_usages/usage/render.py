"""Rich-based rendering for per-account usage reports.

Returns :class:`rich.console.RenderableType` values so callers can
nest them in panels, tables, or print them directly. The braille
progress-bar aesthetic from cc-usage.py is preserved as a custom
renderable — Rich's stock :class:`rich.progress.BarColumn` uses
rectangular blocks which look bulky for this multi-line layout.
"""

import re
import shlex
from collections.abc import Sequence
from datetime import datetime

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import (
    FULL_HEADER_MIN_WIDTH,
    PROVIDER_COLORS,
    brand_header,
)
from sidekick_usages.core.models import UsageReport, UsageWindow
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.activity_render import (
    activity_failure_label,
    activity_text,
)
from sidekick_usages.usage.legacy_render import (
    PLAN_COLORS,
    account_header,
    usage_report,
)
from sidekick_usages.usage.models import (
    AccountUsage,
    AuthenticationFailure,
    CompleteTokenActivity,
    FailedTokenActivity,
    FetchFailure,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PartialTokenActivity,
    PersistenceFailure,
    ProviderTokenActivity,
    RateLimitFailure,
    RefreshRejectedFailure,
    TokenActivityFailureKind,
    TokenActivityIssue,
    UsageCheckResult,
)
from sidekick_usages.usage.reset_display import compact_reset_text

#: Heat bands as (lower-bound-inclusive percent, fg hex, bg hex).
#: Thresholds match the legacy ``_utilization_color`` bands.
_HEAT_BANDS: list[tuple[int, str, str]] = [
    (90, "#ffe6e6", "#b03030"),
    (70, "#fff4e0", "#9c6f12"),
    (40, "#e2fbff", "#1b6a87"),
    (1, "#dfffe9", "#1d5e35"),
]

#: Foreground for a zero-utilization (idle) cell — no fill.
_IDLE_FG = "grey39"

#: Foreground/background for a present-but-zero (0%) utilization cell.
_ZERO_FG = "#cdd3d8"
_ZERO_BG = "#353a40"

#: Fixed width of one window tile.
_TILE_WIDTH = 6

#: Color of the ``│`` rule separating primary from a named-group column.
_RULE_STYLE = "#356f78"

#: Corners + dashes + spaces framing a title/subtitle on the panel border.
#: ``Panel`` measurement ignores the subtitle, so the panel's minimum width
#: is floored by ``len(label) + _PANEL_CHROME`` to keep it from truncating.
_PANEL_CHROME = 6

#: Matches a window length token such as ``5h`` or ``7d``.
_LENGTH_RE = re.compile(r"\d+[hd]")


def _classify_window(name: str) -> tuple[str, str]:
    """Split a window name into ``(length, group)``.

    The length is the first ``\\d+[hd]`` token anywhere in the name;
    the remaining text, trimmed, is the group label (``""`` = the
    provider's main limit; e.g. ``"Spark"`` / ``"Opus"`` for named
    groups). No hardcoded provider tables.

    :param name: Raw ``UsageWindow.name``.
    :return: ``(length, group)``.
    """
    match = _LENGTH_RE.search(name)
    if match is None:
        return (name.strip(), "")
    length = match.group(0)
    group = (name[: match.start()] + name[match.end() :]).strip()
    return (length, group)


def _length_hours(length: str) -> int:
    """Return a sort key (in hours) for a length token.

    :param length: A token like ``"5h"`` or ``"7d"``.
    :return: Hours (``"7d"`` -> 168), or 0 if unparseable.
    """
    match = _LENGTH_RE.fullmatch(length)
    if match is None:
        return 0
    value = int(length[:-1])
    return value * 24 if length[-1] == "d" else value


def _heat_band(pct: int) -> tuple[str, str] | None:
    """Return ``(fg, bg)`` for a utilization percent, or None at 0.

    :param pct: Rounded utilization 0-100.
    :return: ``(fg_hex, bg_hex)`` for a filled tile, or ``None`` for
        a zero cell (rendered as a fill-less ``·``).
    """
    for threshold, fg, bg in _HEAT_BANDS:
        if pct >= threshold:
            return (fg, bg)
    return None


def _heat_tile(pct: int) -> Text:
    """Build one fixed-width heat tile.

    :param pct: Rounded utilization 0-100.
    :return: A ``Text`` of width ``_TILE_WIDTH``: a neutral-grey
        centered ``0%`` at 0, otherwise ``NN%`` centered on the band
        color.
    """
    band = _heat_band(pct)
    if band is None:
        return Text(
            f"{'0%':^{_TILE_WIDTH}}", style=f"{_ZERO_FG} on {_ZERO_BG}"
        )
    fg, bg = band
    return Text(f"{f'{pct}%':^{_TILE_WIDTH}}", style=f"{fg} on {bg}")


def _reset_cell(
    reset_at: datetime | None,
    reference_time: datetime,
) -> Text:
    """Build one fixed-width, dim, centered reset-countdown cell.

    :param reset_at: Aware provider-normalized reset time, if known.
    :param reference_time: Aware wall time for relative formatting.
    :return: A ``Text`` of width ``_TILE_WIDTH``.
    """
    return Text(
        f"{compact_reset_text(reset_at, reference_time):^{_TILE_WIDTH}}",
        style="grey42",
    )


def _dot(provider_id: ProviderId) -> Text:
    return Text("●", style=PROVIDER_COLORS.get(provider_id, "dim"))


def _plan_text(plan: str) -> Text:
    """Plan chip, suppressed for empty/unknown (matches legacy tag)."""
    if not plan or plan == "unknown":
        return Text("")
    return Text(plan, style=PLAN_COLORS.get(plan, "grey42"))


def _panel_columns(
    reports: list[UsageReport],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Derive the column model for one provider from live data.

    :return: ``(primary_lengths, named_groups)`` where primary are the
        main-group lengths (aligned ``5h``/``7d`` columns) and each
        named group is ``(label, lengths)``. Lengths sorted ascending.
    """
    main: dict[str, int] = {}
    groups: dict[str, dict[str, int]] = {}
    for report in reports:
        for window in report.windows:
            length, group = _classify_window(window.name)
            hours = _length_hours(length)
            if group == "":
                main[length] = hours
            else:
                groups.setdefault(group, {})[length] = hours
    primary = sorted(main, key=lambda x: main[x])
    named = [
        (group, sorted(lengths, key=lambda x: lengths[x]))
        for group, lengths in sorted(groups.items())
    ]
    return primary, named


def _window_index(report: UsageReport) -> dict[tuple[str, str], UsageWindow]:
    """Map ``(group, length) -> window`` for one report."""
    index: dict[tuple[str, str], UsageWindow] = {}
    for window in report.windows:
        length, group = _classify_window(window.name)
        index[(group, length)] = window
    return index


def _util_cell(window: UsageWindow | None) -> Text:
    if window is None:
        return Text("")
    return _heat_tile(round(window.utilization))


def _reset_or_blank(
    window: UsageWindow | None,
    reference_time: datetime,
) -> Text:
    if window is None:
        return Text("")
    return _reset_cell(window.resets_at, reference_time)


def _rule_cell() -> Text:
    """The ``│`` separating the primary tiles from a named-group column."""
    return Text("│", style=_RULE_STYLE)


def _model_width(name: str, n_lengths: int) -> int:
    """Width of a named group's MODEL column.

    Wide enough for either the full model name or its length tiles
    (so a long name like ``GPT-5.3-Codex-Spark`` is never truncated;
    Rich has no colspan, so the name lives in one wide column).

    :param name: The group/model label shown as the caption.
    :param n_lengths: How many length tiles sit under the caption.
    :return: ``max(len(name), tiles)`` in cells.
    """
    tiles = _TILE_WIDTH * n_lengths + 2 * (n_lengths - 1)
    return max(len(name), tiles)


def _model_subgrid(cells: list[Text]) -> Table:
    """Lay out one named group's length cells inside a MODEL cell.

    :param cells: One ``Text`` per length (label, tile, or reset).
    :return: A borderless ``Table.grid`` of centered ``_TILE_WIDTH``
        columns matching the primary tile spacing.
    """
    grid = Table.grid(padding=(0, 1))
    for _ in cells:
        grid.add_column(width=_TILE_WIDTH, justify="center")
    grid.add_row(*cells)
    return grid


def _build_table(
    namew: int,
    primary: list[str],
    named: list[tuple[str, list[str]]],
) -> Table:
    table = Table(
        box=None,
        show_header=False,  # header is added manually as a styled row
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column(width=1)  # dot
    table.add_column(width=namew)  # name
    table.add_column(width=4)  # plan
    for _length in primary:
        table.add_column(width=_TILE_WIDTH, justify="center")
    for group, lengths in named:
        table.add_column(width=1, justify="center")  # rule
        table.add_column(
            width=_model_width(group, len(lengths)), justify="left"
        )  # MODEL (wide)
    blank = Text("")
    if named:
        # Caption row: the model name sits above its tiles; the rule
        # cell is blank so no ``│`` is drawn on this row.
        caption: list[Text] = [blank, blank, blank]
        caption.extend(blank for _ in primary)
        for group, _lengths in named:
            caption.append(blank)
            caption.append(Text(group, style="grey46"))
        table.add_row(*caption)
        # Blank line separating the model caption from the 5h/7d header.
        n_cols = 3 + len(primary) + 2 * len(named)
        table.add_row(*([blank] * n_cols))
    header: list[RenderableType] = [blank, blank, blank]
    header.extend(Text(length, style="grey42") for length in primary)
    for _group, lengths in named:
        header.append(_rule_cell())
        header.append(
            _model_subgrid(
                [Text(length, style="grey42") for length in lengths]
            )
        )
    table.add_row(*header)
    return table


def _provider_panel(
    provider_id: ProviderId,
    usages: list[AccountUsage],
    failures: list[FetchFailure],
    namew: int,
    activity: ProviderTokenActivity | None,
    reference_time: datetime,
) -> Panel:
    blocks: list[RenderableType] = []
    if usages:
        primary, named = _panel_columns([usage.report for usage in usages])
        n_cols = 3 + len(primary) + 2 * len(named)
        table = _build_table(namew, primary, named)
        for position, usage in enumerate(usages):
            if position:
                table.add_row(*([Text("")] * n_cols))
            index = _window_index(usage.report)
            util_row: list[RenderableType] = [
                _dot(provider_id),
                Text(usage.label, style="grey85"),
                _plan_text(usage.plan),
            ]
            reset_row: list[RenderableType] = [Text(""), Text(""), Text("")]
            for length in primary:
                window = index.get(("", length))
                util_row.append(_util_cell(window))
                reset_row.append(_reset_or_blank(window, reference_time))
            for group, lengths in named:
                util_row.append(_rule_cell())
                util_row.append(
                    _model_subgrid(
                        [
                            _util_cell(index.get((group, length)))
                            for length in lengths
                        ]
                    )
                )
                reset_row.append(_rule_cell())
                reset_row.append(
                    _model_subgrid(
                        [
                            _reset_or_blank(
                                index.get((group, length)),
                                reference_time,
                            )
                            for length in lengths
                        ]
                    )
                )
            table.add_row(*util_row)
            table.add_row(*reset_row)
        blocks.append(table)
    if failures:
        if blocks:
            blocks.append(Text(""))  # gap between successes and failures
        blocks.append(_error_table(provider_id, failures, namew))
    activity_issues = _account_activity_issues(activity)
    if activity_issues:
        if blocks:
            blocks.append(Text(""))
        blocks.append(
            _activity_issue_table(provider_id, activity_issues, namew)
        )
    content: RenderableType = blocks[0] if len(blocks) == 1 else Group(*blocks)
    color = PROVIDER_COLORS.get(provider_id, "white")
    account_count = len(usages) + len(failures)
    account_noun = "account" if account_count == 1 else "accounts"
    title = Text()
    title.append(provider_id.upper(), style=f"bold {color}")
    title.append(
        f" · {account_count} {account_noun}",
        style="grey54",
    )
    subtitle = (
        activity_text(activity, compact=False)
        if activity is not None
        else None
    )
    return Panel(
        content,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=color,
        padding=(1, 2),
        expand=False,
    )


def _failure_copy(failure: FetchFailure) -> tuple[str, tuple[str, ...]]:
    """Map one typed application failure to human recovery copy."""
    message_lines = tuple(failure.message.splitlines())
    if isinstance(
        failure,
        AuthenticationFailure | RefreshRejectedFailure,
    ):
        provider_name = {
            ProviderId.CLAUDE: "Claude Code",
            ProviderId.CODEX: "Codex CLI",
        }[failure.provider_id]
        command = shlex.join(["sidekick-usages", "refresh", failure.label])
        return (
            "token expired",
            (
                *message_lines,
                f"Log in to {provider_name} again, then run:",
                command,
            ),
        )
    if isinstance(failure, InvalidExpiryFailure):
        command = shlex.join(["sidekick-usages", "refresh", failure.label])
        return "invalid expiry", (*message_lines, command)
    if isinstance(failure, ForbiddenFailure):
        detail = list(message_lines)
        if failure.required_scope is not None:
            detail.append(f"Required scope: {failure.required_scope}.")
        return "forbidden", tuple(detail)
    if isinstance(failure, RateLimitFailure):
        detail = list(message_lines)
        if failure.retry_after_seconds is not None:
            detail.append(
                f"Retry after {failure.retry_after_seconds} seconds."
            )
        return "rate limited", tuple(detail)
    if isinstance(failure, PersistenceFailure):
        return (
            "state not saved",
            (
                "Usage was withheld because account changes were not durable.",
                *message_lines,
            ),
        )
    return "error", message_lines


def _error_table(
    provider_id: ProviderId,
    failures: list[FetchFailure],
    namew: int,
) -> Table:
    """Build the ``[dot, name, plan, rest]`` failure sub-table.

    Dot/name/plan column widths match :func:`_build_table` so failure
    rows align under the success matrix. ``rest`` stacks the status
    line and any recovery detail lines.
    """
    rows: list[tuple[FetchFailure, Group]] = []
    rest_w = 1
    for failure in failures:
        status_label, detail_lines = _failure_copy(failure)
        status = Text(f"⚠ {status_label}", style="yellow")
        detail = [Text(line, style="grey54") for line in detail_lines]
        rest_w = max(rest_w, status.cell_len, *(t.cell_len for t in detail))
        rows.append((failure, Group(status, *detail)))
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
    table.add_column(width=1)  # dot
    table.add_column(width=namew)  # name
    table.add_column(width=4)  # plan
    table.add_column(width=rest_w, justify="left")  # rest
    for position, (failure, rest) in enumerate(rows):
        if position:
            table.add_row(Text(""), Text(""), Text(""), Text(""))
        table.add_row(
            _dot(provider_id),
            Text(failure.label, style="grey85"),
            _plan_text(failure.plan),
            rest,
        )
    return table


def _activity_issue_copy(
    provider_id: ProviderId,
    issue: TokenActivityIssue,
) -> tuple[str, ...]:
    """Return safe recovery detail for one account activity issue."""
    if (
        issue.kind is not TokenActivityFailureKind.AUTHENTICATION
        or issue.label is None
    ):
        return ()
    provider_name = {
        ProviderId.CLAUDE: "Claude Code",
        ProviderId.CODEX: "Codex CLI",
    }[provider_id]
    command = shlex.join(["sidekick-usages", "refresh", issue.label])
    return (
        f"Log in to {provider_name} again, then run:",
        command,
    )


def _account_activity_issues(
    activity: ProviderTokenActivity | None,
) -> tuple[TokenActivityIssue, ...]:
    """Return only account-scoped issues suitable for warning rows."""
    if not isinstance(
        activity,
        CompleteTokenActivity | PartialTokenActivity | FailedTokenActivity,
    ):
        return ()
    return tuple(issue for issue in activity.issues if issue.label is not None)


def _activity_issue_table(
    provider_id: ProviderId,
    issues: tuple[TokenActivityIssue, ...],
    namew: int,
) -> Table:
    """Build account-aligned warning rows for activity read failures."""
    rows: list[tuple[TokenActivityIssue, Group]] = []
    rest_w = 1
    for issue in issues:
        status = Text(
            f"⚠ {activity_failure_label(issue.kind)}",
            style="yellow",
        )
        detail = [
            Text(line, style="grey54")
            for line in _activity_issue_copy(provider_id, issue)
        ]
        rest_w = max(rest_w, status.cell_len, *(t.cell_len for t in detail))
        rows.append((issue, Group(status, *detail)))
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
    table.add_column(width=1)
    table.add_column(width=namew)
    table.add_column(width=4)
    table.add_column(width=rest_w, justify="left")
    for position, (issue, rest) in enumerate(rows):
        if position:
            table.add_row(Text(""), Text(""), Text(""), Text(""))
        if issue.label is None:
            raise ValueError("Account activity issue requires a label.")
        table.add_row(
            _dot(provider_id),
            Text(issue.label, style="grey85"),
            Text(""),
            rest,
        )
    return table


def _panel_min_width(measure: Console, panel: Panel) -> int:
    """Natural width of a panel, floored by its title/subtitle.

    ``Panel`` measurement ignores the title/subtitle text, so a long
    subtitle would otherwise be truncated once the panel is pinned to
    its content width. Floor the result by ``label + _PANEL_CHROME``
    for each border label so neither is clipped.

    :param measure: A wide throwaway ``Console`` used only to measure.
    :param panel: A panel built with ``expand=False`` (natural width).
    :return: The minimum width that fits content, title, and subtitle.
    """
    width = measure.measure(panel).maximum
    for label in (panel.title, panel.subtitle):
        if label is not None:
            width = max(width, measure.measure(label).maximum + _PANEL_CHROME)
    return width


def _legend() -> Text:
    legend = Text()
    for label, sample in (
        ("<40", 20),
        ("40-69", 55),
        ("70-89", 80),
        ("≥90", 95),
    ):
        band = _heat_band(sample)
        fg, bg = band if band else (_IDLE_FG, "default")
        legend.append(f" {label} ", style=f"{fg} on {bg}")
        legend.append("  ")
    legend.append("   dim = resets in", style="grey42")
    return legend


def _provider_order(
    usages: Sequence[AccountUsage],
    failures: Sequence[FetchFailure] = (),
) -> list[ProviderId]:
    order: list[ProviderId] = []
    provider_ids = (
        *(usage.provider_id for usage in usages),
        *(failure.provider_id for failure in failures),
    )
    for provider_id in provider_ids:
        if provider_id not in order:
            order.append(provider_id)
    return order


def _failure_block(failure: FetchFailure) -> Group:
    """Stacked (non-panel) failure block for the legacy narrow view."""
    status, detail = _failure_copy(failure)
    lines: list[RenderableType] = [
        account_header(
            failure.label,
            failure.provider_id,
            failure.plan,
        ),
        Text(f"  ⚠ {status}", style="yellow"),
    ]
    lines.extend(Text(f"  {line}", style="grey54") for line in detail)
    return Group(*lines)


def _activity_issue_block(
    provider_id: ProviderId,
    issue: TokenActivityIssue,
    plan: str,
) -> Group:
    """Stack one activity warning in the narrow fallback."""
    if issue.label is None:
        raise ValueError("Account activity issue requires a label.")
    lines: list[RenderableType] = [
        account_header(issue.label, provider_id, plan),
        Text(f"  ⚠ {activity_failure_label(issue.kind)}", style="yellow"),
    ]
    lines.extend(
        Text(f"  {line}", style="grey54")
        for line in _activity_issue_copy(provider_id, issue)
    )
    return Group(*lines)


def _legacy_activity_blocks(
    result: UsageCheckResult,
) -> list[RenderableType]:
    """Build compact provider activity summaries and warning blocks."""
    blocks: list[RenderableType] = []
    activities = {
        activity.provider_id: activity for activity in result.activities
    }
    plans = {
        (item.provider_id, item.label): item.plan
        for item in (*result.usages, *result.failures)
    }
    for provider_id in _provider_order(result.usages, result.failures):
        activity = activities.get(provider_id)
        if activity is None:
            continue
        if blocks:
            blocks.append(Text(""))
        line = Text()
        color = PROVIDER_COLORS.get(provider_id, "white")
        line.append(provider_id.upper(), style=f"bold {color}")
        line.append(" · ", style="grey54")
        line.append_text(activity_text(activity, compact=True))
        blocks.append(line)
        for issue in _account_activity_issues(activity):
            if blocks:
                blocks.append(Text(""))
            if issue.label is None:
                raise ValueError("Account activity issue requires a label.")
            blocks.append(
                _activity_issue_block(
                    provider_id,
                    issue,
                    plans.get((provider_id, issue.label), "unknown"),
                )
            )
    return blocks


def _legacy_overview(
    result: UsageCheckResult,
) -> RenderableType:
    """Stacked per-account fallback for narrow terminals (no wrap)."""
    blocks: list[RenderableType] = []
    for index, usage in enumerate(result.usages):
        if index:
            blocks.append(Text(""))
        blocks.append(usage_report(usage, result.reference_time))
    for failure in result.failures:
        if blocks:
            blocks.append(Text(""))
        blocks.append(_failure_block(failure))
    activity_blocks = _legacy_activity_blocks(result)
    if blocks and activity_blocks:
        blocks.append(Text(""))
    blocks.extend(activity_blocks)
    return Group(*blocks)


def usage_overview(
    result: UsageCheckResult,
    *,
    width: int,
) -> RenderableType:
    """Render all accounts as provider-grouped framed heat panels.

    :param result: Completed usage rows, failures, and token activity.
    :param width: Target terminal width; below the binding panel
        width the layout degrades to the legacy stacked view.
    :return: A Rich renderable.
    """
    if not result.usages and not result.failures:
        return Text("No usage to display.", style="dim")
    labels = [usage.label for usage in result.usages]
    labels.extend(failure.label for failure in result.failures)
    namew = max(len(s) for s in labels)
    order = _provider_order(result.usages, result.failures)
    activities = {
        activity.provider_id: activity for activity in result.activities
    }
    measure = Console(width=10_000)
    panels = [
        _provider_panel(
            pid,
            [usage for usage in result.usages if usage.provider_id == pid],
            [
                failure
                for failure in result.failures
                if failure.provider_id == pid
            ],
            namew,
            activities.get(pid),
            result.reference_time,
        )
        for pid in order
    ]
    required = max(
        FULL_HEADER_MIN_WIDTH,
        *(_panel_min_width(measure, panel) for panel in panels),
    )
    if width < required:
        return Group(
            brand_header(width),
            Text(""),
            _legacy_overview(result),
        )
    for panel in panels:
        panel.expand = True
        panel.width = required
    parts: list[RenderableType] = [
        Text(""),  # leading blank line — separate the TUI from the prompt
        brand_header(required),
        Text(""),
    ]
    for panel in panels:
        parts.append(panel)
        parts.append(Text(""))
    parts.append(_legend())
    parts.append(Text(""))  # Separate the TUI from the next prompt.
    return Group(*parts)
