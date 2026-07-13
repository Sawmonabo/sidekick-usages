import io
from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pytest
from rich.console import Console

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
from sidekick_usages.usage import (
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
    render,
)
from sidekick_usages.usage.reset_display import compact_reset_text
from tests.test_support import REFERENCE_TIME


def _time_after(**delta: float) -> datetime:
    return REFERENCE_TIME + timedelta(**delta)


def test_heat_band_picks_inclusive_lower_bounds() -> None:
    assert render._heat_band(90) == ("#ffe6e6", "#b03030")
    assert render._heat_band(89) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(70) == ("#fff4e0", "#9c6f12")
    assert render._heat_band(40) == ("#e2fbff", "#1b6a87")
    assert render._heat_band(1) == ("#dfffe9", "#1d5e35")
    assert render._heat_band(0) is None


def test_heat_tile_zero_is_grey_filled_percent() -> None:
    tile = render._heat_tile(0)
    assert tile.plain == f"{'0%':^{render._TILE_WIDTH}}"
    assert tile.style == f"{render._ZERO_FG} on {render._ZERO_BG}"


def test_heat_tile_nonzero_is_centered_percent_on_band() -> None:
    tile = render._heat_tile(94)
    assert tile.plain == f"{'94%':^{render._TILE_WIDTH}}"
    assert tile.style == "#ffe6e6 on #b03030"


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


def test_reset_cell_is_centered_dim() -> None:
    cell = render._reset_cell(
        _time_after(hours=3, minutes=50),
        REFERENCE_TIME,
    )
    assert cell.plain == f"{'3h 50m':^{render._TILE_WIDTH}}"
    assert cell.style == "grey42"
    assert render._reset_cell(None, REFERENCE_TIME).plain == (
        f"{'':^{render._TILE_WIDTH}}"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("5h", ("5h", "")),
        ("7d", ("7d", "")),
        ("7d Opus", ("7d", "Opus")),
        ("7d OAuth", ("7d", "OAuth")),
        ("Spark 5h", ("5h", "Spark")),
        ("Spark 7d", ("7d", "Spark")),
    ],
)
def test_classify_window(name: str, expected: tuple[str, str]) -> None:
    assert render._classify_window(name) == expected


def test_length_hours_orders_5h_before_7d() -> None:
    assert render._length_hours("5h") < render._length_hours("7d")


def _usage(
    label: str,
    report: UsageReport,
    provider: str = "claude",
    plan: str = "max",
) -> AccountUsage:
    return AccountUsage(
        label=AccountLabel(label),
        provider_id=ProviderId(provider),
        plan=plan,
        report=report,
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

#: The documented panel floor (spec §8/§10). The Framed-Panels redesign
#: must render as real panels — not the narrow fallback — for the worst-case
#: store at this width. If a change pushes the binding panel width past it,
#: the floor guard below fails instead of the layout silently degrading.
_PANEL_FLOOR = 85
_NARROW_TEST_WIDTH = 40


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
        render.usage_overview(
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


def test_panels_share_one_width() -> None:
    # measure-then-pin: every provider panel is pinned to the single
    # binding width, so all panel border/interior lines share one right edge.
    out = _render_at(200, _worst_case_usages())
    widths = _panel_line_widths(out)
    assert len(widths) == 1


def test_worst_case_renders_as_panels_at_floor() -> None:
    # The reserved worst-case fixture (30-char name + Spark block) must render
    # as framed panels at the documented floor, with nothing wrapping past
    # the frame and the longest account name intact on one row. Fails if the
    # binding width grows past the floor (a real regression); still passes if
    # the layout gets tighter (an improvement must not break the guard).
    out = _render_at(_PANEL_FLOOR, _worst_case_usages())
    assert "╭─ CLAUDE · 3 accounts ─" in out  # panel path, not narrow
    assert "╭─ CODEX · 2 accounts ─" in out
    assert "since Dec 28, 2025" in out
    assert "since Apr 7, 2026" in out
    assert max(len(line) for line in out.split("\n")) <= _PANEL_FLOOR
    assert "long.account.name@example.test" in out


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


def test_overview_degrades_below_floor_to_narrow() -> None:
    # Well below the binding panel width the renderer falls back to the
    # narrow stacked view instead of squeezing/wrapping the panels.
    # Discriminator: the uppercase panel title only exists on the panel
    # path; the narrow tag uses the lowercase provider id. Branding keeps the
    # complete robot but drops the wide product copy.
    out = _render_at(70, _worst_case_usages())
    assert "╭─ CLAUDE" not in out
    assert ".--┴-┴--.  sidekick usages" in out
    assert "A multi-account usage dashboard" not in out
    assert "long.account.name@example.test" in out


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
        render.usage_overview(
            _result(usages, activities=activities),
            width=200,
        )
    )
    out = buf.getvalue()
    assert "…" not in out
    assert "999,000,000 tokens" in out


def test_failure_renders_in_provider_panel() -> None:
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
    out = _render_at(200, usages, failures=failures)
    assert "⚠ token expired" in out
    assert "Log in to Codex CLI again, then run:" in out
    assert "sidekick-usages refresh long.account.name@example.test" in out
    assert "╭─ CODEX · 2 accounts ─" in out
    assert "needs attention" not in out
    first = next(line for line in out.splitlines() if line.strip())
    assert first.strip() == "o"


@pytest.mark.parametrize(
    ("credential_kind", "cause", "expected_lines"),
    [
        (
            CredentialRecoveryKind.CLAUDE_SETUP_TOKEN,
            "Claude rejected the saved setup token.",
            (
                "Claude rejected the saved setup token.",
                "Run: sidekick-usages claude setup-token --label "
                "'team account' --force",
            ),
        ),
        (
            CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN,
            "Claude rejected the saved subscription login.",
            (
                "Claude rejected the saved subscription login.",
                "Sign in to that Claude account, then run:",
                "sidekick-usages refresh 'team account'",
            ),
        ),
    ],
)
def test_claude_auth_recovery_has_one_mode_appropriate_action(
    credential_kind: CredentialRecoveryKind,
    cause: str,
    expected_lines: tuple[str, ...],
) -> None:
    failure = AuthenticationFailure(
        label=AccountLabel("team account"),
        provider_id=ProviderId.CLAUDE,
        plan="max",
        message=cause,
        credential_kind=credential_kind,
    )

    status, detail = render._failure_copy(failure)

    assert status == "authentication failed"
    assert detail == expected_lines
    assert sum("sidekick-usages" in line for line in detail) == 1
    assert "log in again" not in "\n".join(detail).lower()


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
        render.usage_overview(
            _result(usages, failures=failures),
            width=200,
        )
    )
    out = buf.getvalue()
    widths = _panel_line_widths(out)
    assert len(widths) == 1
    assert "sidekick-usages refresh long.account.name@example.test" in out


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
        render.usage_overview(
            _result(usages, failures=failures),
            width=_NARROW_TEST_WIDTH,
        )
    )
    out = buf.getvalue()
    assert "token expired" in out
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
            selected_accounts=2,
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
        render.usage_overview(
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
    assert "sidekick-usages refresh 'profile failed'" in out.replace("\n", "")
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
