import io
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from prompt_toolkit.formatted_text import (
    ANSI,
    fragment_list_to_text,
    to_formatted_text,
)
from rich.console import Console

from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardFooter,
    DashboardNavigationKind,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.models import (
    AccountUsage,
    AuthenticationFailure,
    CompleteTokenActivity,
    CredentialRecoveryKind,
    FailedTokenActivity,
    FetchFailure,
    PartialTokenActivity,
    PersistenceFailure,
    TokenActivityFailureKind,
    TokenActivityIssue,
    UsageCheckResult,
)
from sidekick_usages.usage.presentation import overview
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard,
)
from sidekick_usages.usage.presentation.dashboard.render.style import (
    ANSI_RESET,
    ansi_style,
    dashboard_color_enabled,
)
from sidekick_usages.usage.presentation.formatting import (
    cell_width,
    compact_reset_text,
    panel_model_width,
)
from sidekick_usages.usage.presentation.theme import (
    UsageTextRole,
    usage_style,
)
from tests.fakes.dashboard.render import (
    FORBIDDEN_SELECTION_LABELS,
    PROGRESS_COPY,
    interactive_dashboard_state,
)
from tests.support.time import REFERENCE_TIME

_ACTIVITIES = (
    CompleteTokenActivity(
        provider_id=ProviderId.CLAUDE,
        summary=TokenActivitySummary(
            total_tokens=903_464_085,
            scope=TokenActivityScope.LOCAL_INSTALLATION,
            since=date(2025, 12, 28),
        ),
    ),
    CompleteTokenActivity(
        provider_id=ProviderId.CODEX,
        summary=TokenActivitySummary(
            total_tokens=7_449_473_297,
            scope=TokenActivityScope.ACCOUNT,
            since=date(2026, 4, 7),
        ),
    ),
)
_PANEL_FLOOR = 85
_NARROW_TEST_WIDTH = 40
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


def _time_after(**delta: float) -> datetime:
    return REFERENCE_TIME + timedelta(**delta)


def test_format_reset_compact_buckets() -> None:
    assert compact_reset_text(None, REFERENCE_TIME) == ""
    assert (
        compact_reset_text(
            _time_after(minutes=-5),
            REFERENCE_TIME,
        )
        == "now"
    )
    assert (
        compact_reset_text(
            _time_after(minutes=45),
            REFERENCE_TIME,
        )
        == "45m"
    )
    assert (
        compact_reset_text(
            _time_after(hours=3, minutes=50),
            REFERENCE_TIME,
        )
        == "3h 50m"
    )
    assert (
        compact_reset_text(
            _time_after(days=1, hours=15),
            REFERENCE_TIME,
        )
        == "1d 15h"
    )


def _usage(
    label: str,
    report: UsageReport,
    provider: str = "claude",
    plan: str = "max",
) -> AccountUsage:
    return AccountUsage(
        account_id=SidekickAccountId("56b5f5b7-2156-42db-9505-00e6d4cc76a0"),
        label=AccountLabel(label),
        provider_id=ProviderId(provider),
        plan=plan,
        report=report,
        fetched_at=REFERENCE_TIME,
        freshness=MetricsFreshness.FRESH,
    )


def _auth_failure(
    label: str = "long.account.name@example.test",
) -> AuthenticationFailure:
    return AuthenticationFailure(
        label=AccountLabel(label),
        provider_id=ProviderId.CODEX,
        plan="pro",
        message="Refresh token unavailable or rejected.",
        credential_kind=CredentialRecoveryKind.CODEX_LOGIN,
    )


def _report(
    *windows: tuple[str, float, datetime | None],
) -> UsageReport:
    return UsageReport(
        windows=tuple(UsageWindow(*w) for w in windows),
        plan="max",
    )


def _worst_case_usages() -> list[AccountUsage]:
    # 3 Claude + 2 Codex; the reserved 30-char name + Spark block is the
    # binding width case.
    reset_at = _time_after(hours=3, minutes=50)
    claude = [
        _usage(
            "short.account@example.test",
            _report(("5h", 94, reset_at), ("7d", 61, reset_at)),
        ),
        _usage(
            "team.account@example.test",
            _report(("5h", 12, reset_at), ("7d", 73, reset_at)),
            plan="team",
        ),
        _usage(
            "third.account@example.test",
            _report(("5h", 40, reset_at), ("7d", 5, reset_at)),
        ),
    ]
    codex = [
        _usage(
            "codex@example.test",
            _report(
                ("5h", 8, reset_at),
                ("7d", 45, reset_at),
                ("GPT-5.3-Codex-Spark 5h", 0, reset_at),
                ("GPT-5.3-Codex-Spark 7d", 0, reset_at),
            ),
            "codex",
            "pro",
        ),
        _usage(
            "long.account.name@example.test",
            _report(
                ("5h", 0, reset_at),
                ("7d", 0, reset_at),
                ("GPT-5.3-Codex-Spark 5h", 0, reset_at),
                ("GPT-5.3-Codex-Spark 7d", 0, reset_at),
            ),
            "codex",
            "pro",
        ),
    ]
    return claude + codex


def _render_at(
    width: int,
    usages: list[AccountUsage],
    *,
    failures: Sequence[FetchFailure] = (),
) -> str:
    buf = io.StringIO()
    # legacy_windows=False keeps Rich's rounded box on every platform.
    # Without it, Windows CI substitutes the square box (┌ for ╭) and the
    # panel-corner assertions below fail.
    console = Console(width=width, file=buf, legacy_windows=False)
    console.print(
        overview.usage_overview(
            _result(usages, failures=failures),
            width=width,
        )
    )
    return buf.getvalue()


def _result(
    usages: Sequence[AccountUsage],
    *,
    failures: Sequence[FetchFailure] = (),
    activities: tuple[
        CompleteTokenActivity | PartialTokenActivity | FailedTokenActivity,
        ...,
    ] = _ACTIVITIES,
) -> UsageCheckResult:
    return UsageCheckResult(
        tuple(usages),
        tuple(failures),
        REFERENCE_TIME,
        activities,
    )


def _render_interactive(width: int) -> tuple[str, str, str]:
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
    return (
        render_dashboard(
            snapshot,
            width=width,
            cursor=cursor,
            footer=footer,
            color=False,
        ),
        render_dashboard(
            priority_snapshot,
            width=width,
            cursor=cursor,
            footer=DashboardFooter(),
            color=False,
        ),
        render_dashboard(
            priority_snapshot,
            width=width,
            cursor=replace(
                cursor,
                account_id=warning_account.account_id,
            ),
            footer=DashboardFooter(),
            color=False,
        ),
    )


@pytest.mark.parametrize(
    ("width", "expected"),
    [(200, "3h 50m"), (70, "(in 3h 50m)")],
)
def test_overview_uses_explicit_reference_time(
    width: int,
    expected: str,
) -> None:
    usages = [
        _usage(
            "fixed-time",
            _report(
                (
                    "5h",
                    1,
                    _time_after(hours=3, minutes=50),
                )
            ),
        )
    ]

    assert expected in _render_at(width, usages)


def _panel_line_widths(out: str) -> set[int]:
    # ``line and`` guards the blank separator lines: "" is a substring
    # of every string, so "" in "╭│╰" is True and would count width 0.
    return {
        len(line) for line in out.split("\n") if line and line[:1] in "╭│╰"
    }


def _assert_theme_palette() -> None:
    for role, expected in _EXPECTED_THEME_COLORS.items():
        theme = usage_style(role)
        assert (theme.foreground, theme.background) == expected
    assert not usage_style(UsageTextRole.ACCOUNT_LABEL).bold
    assert usage_style(UsageTextRole.ADVISORY).dim


def test_panels_share_one_width() -> None:
    # measure-then-pin: every provider panel is pinned to the single
    # binding width, so all panel border/interior lines share one right edge.
    out = _render_at(200, _worst_case_usages())
    widths = _panel_line_widths(out)
    assert len(widths) == 1


def test_interactive_wide_render_preserves_dashboard_contract() -> None:
    out, unavailable, setup = _render_interactive(_INTERACTIVE_WIDE_WIDTH)
    cursor = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"

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
    assert "903,464,085 tokens" in out
    assert "7,449,473,297 tokens" in out
    assert "since Dec 28, 2025" in out
    assert "since Apr 7, 2026" in out
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
    assert _panel_line_widths(plain) == _panel_line_widths(out)
    assert all(
        copy in plain for copy in ("Press Enter to repair and", "use it.")
    )
    assert dashboard_color_enabled({}, terminal=True)
    assert not dashboard_color_enabled({"NO_COLOR": ""}, terminal=True)


def test_overview_shows_robot_masthead_and_provider_titles() -> None:
    out = _render_at(120, _worst_case_usages())
    assert "      o" in out
    assert "     .-." in out
    assert "  .--┴-┴--.    sidekick usages" in out
    assert (
        "  | O   O |   >> A multi-account usage dashboard for "
        "Claude Code and Codex CLI."
    ) in out
    assert (
        "  | ||||| |   >> Limits + resets + account status, one terminal."
        in out
    )
    assert "  '--___--'" in out
    assert "5 accounts · 2 providers" not in out
    assert "╭─ CLAUDE · 3 accounts ─" in out
    assert "╭─ CODEX · 2 accounts ─" in out
    assert "GPT-5.3-Codex-Spark" in out


@pytest.mark.parametrize(
    (
        "width",
        "claude_total",
        "codex_total",
    ),
    [
        (
            120,
            "903,464,085 tokens",
            "7,449,473,297 tokens",
        ),
        (_NARROW_TEST_WIDTH, "903.46M tokens", "7.449B tokens"),
    ],
)
def test_activity_totals_share_tokens_and_since_footer_contract(
    width: int,
    claude_total: str,
    codex_total: str,
) -> None:
    out = _render_at(width, _worst_case_usages())

    assert claude_total in out
    assert codex_total in out
    assert "since Dec 28, 2025" in out
    assert "since Apr 7, 2026" in out
    assert "local" not in out
    assert "known tokens" not in out
    assert "output" not in out
    if width == _NARROW_TEST_WIDTH:
        lines = out.splitlines()
        claude = lines.index("CLAUDE · 903.46M tokens")
        codex = lines.index("CODEX · 7.449B tokens")
        assert lines[claude + 1] == "         since Dec 28, 2025"
        assert lines[codex + 1] == "        since Apr 7, 2026"
        assert max(map(len, lines)) <= width


def test_provider_title_uses_singular_account_count() -> None:
    usages = [
        _usage(
            "only",
            _report(
                ("5h", 5, _time_after(hours=1)),
                ("7d", 9, _time_after(days=1)),
            ),
            "codex",
            "pro",
        )
    ]
    out = _render_at(120, usages)
    assert "╭─ CODEX · 1 account ─" in out
    assert "CODEX · 1 accounts" not in out


def test_interactive_narrow_render_preserves_dashboard_contract() -> None:
    out, unavailable, setup = _render_interactive(_INTERACTIVE_NARROW_WIDTH)
    cursor = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"

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


def test_overview_empty_pairs() -> None:
    out = _render_at(80, [])
    assert "No usage" in out


def test_named_group_caption_row_and_rule_present() -> None:
    out = _render_at(200, _worst_case_usages())
    cap = next(
        line for line in out.split("\n") if "GPT-5.3-Codex-Spark" in line
    )
    assert "%" not in cap  # caption sits above the tiles, not inline
    assert "│" in out  # the model rule is drawn on data rows
    group = "模型名稱"
    assert panel_model_width(group, 1) == cell_width(group)
    assert cell_width(group) > len(group)


def test_subtitle_not_truncated_when_wider_than_content() -> None:
    usages = [
        _usage(
            "x",
            _report(
                ("5h", 5, _time_after(hours=1)),
                ("7d", 9, _time_after(days=1)),
            ),
            "codex",
            "pro",
        )
    ]
    activities = (
        CompleteTokenActivity(
            provider_id=ProviderId.CODEX,
            summary=TokenActivitySummary(
                total_tokens=999_000_000,
                scope=TokenActivityScope.ACCOUNT,
            ),
        ),
    )
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        overview.usage_overview(
            _result(usages, activities=activities),
            width=200,
        )
    )
    out = buf.getvalue()
    assert "…" not in out
    assert "999,000,000 tokens" in out


def test_stale_usage_and_failure_render_as_one_timestamped_account() -> None:
    iso = _time_after(hours=3)
    usages = [
        replace(
            _usage(
                "acct-stale",
                _report(("5h", 8, iso), ("7d", 45, iso)),
                "codex",
                "pro",
            ),
            fetched_at=REFERENCE_TIME - timedelta(hours=1),
            freshness=MetricsFreshness.STALE,
        )
    ]
    failures = [_auth_failure("acct-stale")]
    out = _render_at(200, usages, failures=failures)
    assert "last known · 2026-06-12T11:34:56.789000+00:00" in out
    assert "⚠ login required" in out
    assert "Run official managed Codex login:" in out
    assert "sidekick-usages codex login acct-stale" in out
    assert "╭─ CODEX · 1 account ─" in out
    assert "needs attention" not in out
    first = next(line for line in out.splitlines() if line.strip())
    assert first.strip() == "o"


@pytest.mark.parametrize("width", [200, _NARROW_TEST_WIDTH])
def test_claude_auth_recovery_fits_normal_and_narrow_layouts(
    width: int,
) -> None:
    failures = [
        AuthenticationFailure(
            label=AccountLabel("team account"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            message="Claude rejected the saved setup token.",
            credential_kind=CredentialRecoveryKind.CLAUDE_SETUP_TOKEN,
        ),
        AuthenticationFailure(
            label=AccountLabel("saved login"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            message="Claude rejected the saved subscription login.",
            credential_kind=(CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN),
        ),
    ]

    out = _render_at(width, [], failures=failures)

    assert max(map(len, out.splitlines())) <= width
    assert "log in again" not in out.lower()


def test_persistence_failure_is_not_rendered_as_successful_usage() -> None:
    failures = [
        PersistenceFailure(
            label=AccountLabel("long.account.name@example.test"),
            provider_id=ProviderId.CODEX,
            plan="pro",
            message="Store replacement failed.",
            persistence_code=PersistenceCode.REPLACE_FAILED,
        )
    ]
    out = _render_at(200, [], failures=failures)
    assert "⚠ state not saved" in out
    assert "Usage was withheld" in out
    assert "╭─ CODEX · 1 account ─" in out
    assert "5h" not in out


def test_failures_widen_shared_panels() -> None:
    iso = _time_after(hours=3)
    usages = [
        _usage(
            "short.account@example.test",
            _report(("5h", 94, iso), ("7d", 61, iso)),
        )
    ]
    failures = [_auth_failure()]
    buf = io.StringIO()
    console = Console(width=200, file=buf)
    console.print(
        overview.usage_overview(
            _result(usages, failures=failures),
            width=200,
        )
    )
    out = buf.getvalue()
    widths = _panel_line_widths(out)
    assert len(widths) == 1
    assert "sidekick-usages codex login long.account.name@example.test" in out


def test_narrow_layout_renders_failures() -> None:
    iso = _time_after(hours=3)
    usages = [
        _usage(
            "acct-ok",
            _report(("5h", 8, iso), ("7d", 45, iso)),
            "codex",
            "pro",
        )
    ]
    failures = [_auth_failure()]
    buf = io.StringIO()
    console = Console(width=_NARROW_TEST_WIDTH, file=buf)
    console.print(
        overview.usage_overview(
            _result(usages, failures=failures),
            width=_NARROW_TEST_WIDTH,
        )
    )
    out = buf.getvalue()
    assert "login required" in out
    assert "7.449B tokens" in out
    assert "since Apr 7, 2026" in out
    assert max(map(len, out.splitlines())) <= _NARROW_TEST_WIDTH


@pytest.mark.parametrize(
    ("width", "token_total"),
    [
        (200, "7,449,473,297 tokens"),
        (_NARROW_TEST_WIDTH, "7.449B tokens"),
    ],
)
def test_partial_activity_keeps_usage_and_actionable_warning(
    width: int,
    token_total: str,
) -> None:
    usages = [
        _usage(
            "known",
            _report(("5h", 8, _time_after(hours=3))),
            "codex",
            "pro",
        ),
        _usage(
            "profile failed",
            _report(("5h", 12, _time_after(hours=4))),
            "codex",
            "pro",
        ),
    ]
    activities = (
        PartialTokenActivity(
            provider_id=ProviderId.CODEX,
            summary=TokenActivitySummary(
                total_tokens=7_449_473_297,
                scope=TokenActivityScope.ACCOUNT,
                since=date(2026, 4, 7),
            ),
            covered_accounts=1,
            saved_accounts=2,
            issues=(
                TokenActivityIssue(
                    kind=TokenActivityFailureKind.AUTHENTICATION,
                    message="Safe application message.",
                    label=AccountLabel("profile failed"),
                ),
            ),
        ),
    )
    buf = io.StringIO()
    Console(width=width, file=buf).print(
        overview.usage_overview(
            _result(usages, activities=activities),
            width=width,
        )
    )
    out = buf.getvalue()

    assert token_total in out
    assert "since Apr 7, 2026" in out
    assert "known tokens" not in out
    assert "known" in out
    assert "profile failed" in out
    assert "token activity authentication failed" in out
    assert "sidekick-usages codex login 'profile failed'" in out.replace(
        "\n", ""
    )
    assert "Safe application message" not in out


def test_panels_have_interior_top_padding() -> None:
    out = _render_at(200, _worst_case_usages())
    lines = out.splitlines()
    tops = [i for i, line in enumerate(lines) if line.lstrip().startswith("╭")]
    assert tops  # at least one panel
    for i in tops:
        inner = lines[i + 1]
        # the row right under the top border is blank between the borders
        assert inner.lstrip().startswith("│")
        assert inner.strip("│ ") == ""


def test_named_panel_separates_caption_from_header() -> None:
    out = _render_at(200, _worst_case_usages())
    lines = out.splitlines()
    cap = next(
        i for i, line in enumerate(lines) if "GPT-5.3-Codex-Spark" in line
    )
    assert lines[cap + 1].strip("│ ") == ""  # blank separator
    assert "5h" in lines[cap + 2]  # header follows
