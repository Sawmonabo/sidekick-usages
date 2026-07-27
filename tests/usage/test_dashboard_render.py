"""Interactive dashboard render contract tests."""

from dataclasses import replace

from prompt_toolkit.formatted_text import (
    ANSI,
    fragment_list_to_text,
    to_formatted_text,
)

from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardFooter,
    DashboardNavigationKind,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard,
)
from sidekick_usages.usage.presentation.dashboard.render.style import (
    ANSI_RESET,
    ansi_style,
    dashboard_color_enabled,
)
from sidekick_usages.usage.presentation.formatting import cell_width
from sidekick_usages.usage.presentation.theme import (
    UsageTextRole,
    usage_style,
)
from tests.fakes.dashboard.render import (
    FORBIDDEN_SELECTION_LABELS,
    PROGRESS_COPY,
    interactive_dashboard_state,
)
from tests.support.terminal import panel_line_widths
from tests.support.time import REFERENCE_TIME

_INTERACTIVE_WIDE_WIDTH = 200
_INTERACTIVE_NARROW_WIDTH = 70
_KEY_FOOTER_TEXT = "↑/↓ or j/k move"
_LOGIN_DETAIL = (
    "Complete the official Claude Code login before using this account."
)
_SETUP_DETAIL = "Enter to connect this account for Claude switching."
_STALE_DETAIL = "Live metrics refresh failed; showing data from 2h 14m ago."
_UNAVAILABLE_DETAIL = (
    "Live metrics refresh failed; no saved metrics available."
)
_CLAUDE_UNREADABLE_DETAIL = (
    "Claude Code login could not be verified; account switching is paused."
)
_CLAUDE_UNSUPPORTED_DETAIL = (
    "Claude Code account verification is unavailable; saved metrics remain "
    "visible."
)
_EXPECTED_THEME_COLORS = {
    UsageTextRole.HEAT_ZERO: ("#cdd3d8", "#353a40"),
    UsageTextRole.HEAT_GREEN: ("#dfffe9", "#1d5e35"),
    UsageTextRole.HEAT_CYAN: ("#e2fbff", "#1b6a87"),
    UsageTextRole.HEAT_YELLOW: ("#fff4e0", "#9c6f12"),
    UsageTextRole.HEAT_RED: ("#ffe6e6", "#b03030"),
    UsageTextRole.ACCOUNT_LABEL: ("#dadada", None),
    UsageTextRole.PANEL_META: ("#8a8a8a", None),
    UsageTextRole.HEADER: ("#6c6c6c", None),
    UsageTextRole.MODEL_CAPTION: ("#767676", None),
    UsageTextRole.ACTIVITY_SINCE: ("#585858", None),
    UsageTextRole.MASTHEAD_DIVIDER: ("#3a3a3a", None),
    UsageTextRole.MODEL_RULE: ("#356f78", None),
    UsageTextRole.FOOTER_HELP: ("#b2b2b2", None),
    UsageTextRole.ADVISORY: ("#b59a55", None),
}


def _render_interactive(
    width: int,
    runtime_state: ProviderRuntimeState,
) -> tuple[str, str, str, str]:
    snapshot, cursor, footer = interactive_dashboard_state(REFERENCE_TIME)
    provider = snapshot.providers[0]
    warning_account = provider.rows[1]
    assert isinstance(warning_account, DashboardAccount)
    priority_snapshot = replace(
        snapshot,
        providers=(
            replace(
                provider,
                rows=(
                    provider.rows[0],
                    replace(
                        warning_account,
                        states=(DashboardActionState.SWITCH_SETUP_REQUIRED,),
                    ),
                ),
            ),
            *snapshot.providers[1:],
        ),
    )
    warning_cursor = replace(
        cursor,
        account_id=warning_account.account_id,
    )
    unavailable_snapshot = replace(
        priority_snapshot,
        providers=(
            replace(
                priority_snapshot.providers[0],
                actions_enabled=False,
            ),
            *priority_snapshot.providers[1:],
        ),
    )
    degraded_snapshot = replace(
        snapshot,
        providers=(
            replace(
                provider,
                runtime_state=runtime_state,
                active_account_id=None,
                actions_enabled=False,
                rows=tuple(
                    replace(row, active=False)
                    for row in provider.rows
                    if isinstance(row, DashboardAccount)
                ),
            ),
            *snapshot.providers[1:],
        ),
    )
    return (
        render_dashboard(
            snapshot,
            width=width,
            cursor=cursor,
            footer=footer,
            color=False,
        ),
        render_dashboard(
            unavailable_snapshot,
            width=width,
            cursor=warning_cursor,
            footer=DashboardFooter(),
            color=False,
        ),
        render_dashboard(
            priority_snapshot,
            width=width,
            cursor=warning_cursor,
            footer=DashboardFooter(),
            color=False,
        ),
        render_dashboard(
            degraded_snapshot,
            width=width,
            cursor=replace(cursor, account_id=None),
            footer=DashboardFooter(),
            color=False,
        ),
    )


def _assert_theme_palette() -> None:
    for role, expected in _EXPECTED_THEME_COLORS.items():
        theme = usage_style(role)
        assert (theme.foreground, theme.background) == expected
    assert not usage_style(UsageTextRole.ACCOUNT_LABEL).bold
    assert usage_style(UsageTextRole.ADVISORY).dim


def test_interactive_wide_render_preserves_dashboard_contract() -> None:
    out, unavailable, setup, degraded = _render_interactive(
        _INTERACTIVE_WIDE_WIDTH,
        ProviderRuntimeState.UNREADABLE,
    )
    cursor = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
    semantic_degraded = " ".join(
        line.strip("│ ") for line in degraded.splitlines()
    )

    assert "      o" in out
    assert "╭─ CLAUDE · 2 accounts ─" in out
    assert "╭─ CODEX · 2 accounts ─" in out
    assert out.count(cursor) == 1
    assert f"{cursor} ●" in out
    assert "work@example.test" in out
    assert "⚠ codex@example.test" not in out
    assert (
        out.count(_LOGIN_DETAIL),
        out.count(_STALE_DETAIL),
        out.count(_UNAVAILABLE_DETAIL),
    ) == (1, 1, 0)
    assert "This external login is not saved in Sidekick." in out
    assert (
        out.count(PROGRESS_COPY),
        out.count(_KEY_FOOTER_TEXT),
        out.index(PROGRESS_COPY) < out.index(_KEY_FOOTER_TEXT),
    ) == (1, 1, True)
    assert (
        unavailable.count(_UNAVAILABLE_DETAIL),
        unavailable.count(_SETUP_DETAIL),
        setup.count(_SETUP_DETAIL),
        setup.count(_UNAVAILABLE_DETAIL),
    ) == (1, 0, 1, 0)
    assert all(
        copy in out
        for copy in (
            "903,464,085 tokens",
            "7,449,473,297 tokens",
            "since Dec 28, 2025",
            "since Apr 7, 2026",
        )
    )
    assert "3h 50m" in out
    assert not any(label in out for label in FORBIDDEN_SELECTION_LABELS)
    lines = out.splitlines()
    work_row = next(
        position
        for position, rendered in enumerate(lines)
        if "work@example.test" in rendered
    )
    work_warning = next(
        position
        for position, rendered in enumerate(lines)
        if _STALE_DETAIL in rendered
    )
    personal_row = next(
        position
        for position, rendered in enumerate(lines)
        if "personal@example.test" in rendered
    )
    personal_warning = next(
        position
        for position, rendered in enumerate(lines)
        if _LOGIN_DETAIL in rendered
    )
    assert (
        semantic_degraded.count(_CLAUDE_UNREADABLE_DETAIL),
        degraded.count(_LOGIN_DETAIL),
        degraded.count(_STALE_DETAIL),
    ) == (1, 1, 1)
    assert (
        semantic_degraded.index("5h")
        < semantic_degraded.index(_CLAUDE_UNREADABLE_DETAIL)
        < semantic_degraded.index("work@example.test")
    )
    assert work_row < work_warning < personal_row < personal_warning
    assert max(len(line) for line in out.splitlines()) <= (
        _INTERACTIVE_WIDE_WIDTH
    )

    snapshot, cursor_state, _ = interactive_dashboard_state(REFERENCE_TIME)
    provider = snapshot.providers[0]
    account = provider.rows[0]
    assert isinstance(account, DashboardAccount)
    assert account.usage is not None
    source_window = account.usage.report.windows[1]
    unsafe_window = replace(
        source_window,
        name="group 99%\x1b[31m 7d",
    )
    unsafe_account = replace(
        account,
        label=AccountLabel("work-99%@example.test"),
        states=(DashboardActionState.REPAIR_REQUIRED,),
        usage=replace(
            account.usage,
            report=replace(
                account.usage.report,
                windows=(unsafe_window,),
            ),
        ),
    )
    unsafe_snapshot = replace(
        snapshot,
        providers=(
            replace(
                provider,
                rows=(unsafe_account, *provider.rows[1:]),
            ),
            *snapshot.providers[1:],
        ),
    )
    unsafe_footer = DashboardFooter(
        navigation=DashboardNavigationKind.KEYS,
        status=DashboardStatus(
            kind=DashboardStatusKind.PROGRESS,
            message="working 99%",
        ),
    )
    plain = render_dashboard(
        unsafe_snapshot,
        width=_INTERACTIVE_WIDE_WIDTH,
        cursor=cursor_state,
        footer=unsafe_footer,
        color=False,
    )
    colored = render_dashboard(
        unsafe_snapshot,
        width=_INTERACTIVE_WIDE_WIDTH,
        cursor=cursor_state,
        footer=unsafe_footer,
        color=True,
    )

    assert fragment_list_to_text(to_formatted_text(ANSI(colored))) == plain
    assert "\x1b[31m" not in plain
    assert "\N{REPLACEMENT CHARACTER}" in plain
    assert (
        f"{ansi_style(UsageTextRole.HEAT_CYAN)} 51%  {ANSI_RESET}" in colored
    )
    assert (
        f"{ansi_style(UsageTextRole.HEAT_RED)}99%{ANSI_RESET}" not in colored
    )
    assert f"{ansi_style(UsageTextRole.RESET)}3h 50m{ANSI_RESET}" in colored
    _assert_theme_palette()
    assert (
        f"{ansi_style(UsageTextRole.ACCOUNT_LABEL)}"
        f"work-99%@example.test{ANSI_RESET}" in colored
    )
    assert panel_line_widths(plain) == panel_line_widths(out)
    assert all(
        copy in plain for copy in ("Press Enter to repair and", "use it.")
    )
    assert dashboard_color_enabled({}, terminal=True)
    assert not dashboard_color_enabled({"NO_COLOR": ""}, terminal=True)


def test_interactive_narrow_render_preserves_dashboard_contract() -> None:
    out, unavailable, setup, degraded = _render_interactive(
        _INTERACTIVE_NARROW_WIDTH,
        ProviderRuntimeState.UNSUPPORTED,
    )
    cursor = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
    semantic_degraded = " ".join(
        line.strip() for line in degraded.splitlines()
    )

    assert "╭─ CLAUDE" not in out
    assert ".--┴-┴--.  sidekick usages" in out
    assert "A multi-account usage dashboard" not in out
    assert "[claude · max]" in out
    assert "[codex · pro]" in out
    assert out.count(cursor) == 1
    assert f"{cursor} ● work@example.test" in out
    assert (
        out.count("Complete the official Claude Code login"),
        out.count(_STALE_DETAIL),
        out.count(_UNAVAILABLE_DETAIL),
    ) == (1, 1, 0)
    assert "External Codex CLI login" in out
    assert "This external login is not saved in Sidekick." in out
    assert (
        out.count(PROGRESS_COPY),
        out.count(_KEY_FOOTER_TEXT),
        out.index(PROGRESS_COPY) < out.index(_KEY_FOOTER_TEXT),
    ) == (1, 1, True)
    assert (
        unavailable.count(_UNAVAILABLE_DETAIL),
        unavailable.count(_SETUP_DETAIL),
        setup.count(_SETUP_DETAIL),
        setup.count(_UNAVAILABLE_DETAIL),
    ) == (1, 0, 1, 0)
    assert "903.46M tokens" in out
    assert "7.449B tokens" in out
    assert "since Dec 28, 2025" in out
    assert "since Apr 7, 2026" in out
    assert "(in 3h 50m)" in out
    assert not any(label in out for label in FORBIDDEN_SELECTION_LABELS)
    assert (
        semantic_degraded.count(_CLAUDE_UNSUPPORTED_DETAIL),
        degraded.count("Complete the official Claude Code login"),
        degraded.count(_STALE_DETAIL),
    ) == (1, 1, 1)
    assert semantic_degraded.index(
        _CLAUDE_UNSUPPORTED_DETAIL
    ) < semantic_degraded.index("work@example.test")
    assert max(len(line) for line in out.splitlines()) <= (
        _INTERACTIVE_NARROW_WIDTH
    )

    snapshot, cursor_state, footer = interactive_dashboard_state(
        REFERENCE_TIME
    )
    provider = snapshot.providers[0]
    account = provider.rows[0]
    assert isinstance(account, DashboardAccount)
    assert account.usage is not None
    no_window_account = replace(
        account,
        usage=replace(
            account.usage,
            report=replace(account.usage.report, windows=()),
        ),
    )
    no_window_snapshot = replace(
        snapshot,
        providers=(
            replace(
                provider,
                rows=(no_window_account, *provider.rows[1:]),
            ),
            *snapshot.providers[1:],
        ),
    )
    width = 3
    narrow = render_dashboard(
        no_window_snapshot,
        width=width,
        cursor=cursor_state,
        footer=footer,
        color=False,
    )
    assert all(cell_width(line) <= width for line in narrow.splitlines())
