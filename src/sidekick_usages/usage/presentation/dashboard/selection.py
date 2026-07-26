"""Cursor, row decoration, and actionable detail projection."""

import shlex
from datetime import datetime
from typing import assert_never

from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardCursor,
    DashboardExternalRow,
    DashboardRow,
)
from sidekick_usages.usage.models import (
    AuthenticationFailure,
    CompleteTokenActivity,
    CredentialRecoveryKind,
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
)

EXTERNAL_ROW_LABELS = {
    ProviderId.CLAUDE: "External Claude Code login",
    ProviderId.CODEX: "External Codex CLI login",
}
CURSOR_GLYPH = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
PROVIDER_NAMES = {
    ProviderId.CLAUDE: "Claude Code",
    ProviderId.CODEX: "Codex CLI",
}
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
PLAN_COLORS: dict[str, str] = {
    "max": "magenta",
    "team": "cyan",
    "pro": "green",
    "plus": "green",
    "enterprise": "yellow",
    "business": "yellow",
}
STATIC_STATE_DETAILS = {
    DashboardActionState.SETUP_TOKEN_REGENERATION: (
        "Generate a new Claude setup token before using this account."
    ),
    DashboardActionState.EXTERNAL_ACTIVE: (
        "This external login is not saved in Sidekick."
    ),
    DashboardActionState.RECONCILIATION_REQUIRED: (
        "Provider login needs reconciliation before account switching."
    ),
    DashboardActionState.PROVIDER_UNSUPPORTED: (
        "Update the provider CLI before using account switching."
    ),
    DashboardActionState.SERVICE_UNAVAILABLE: (
        "Account actions are unavailable until Sidekick is ready."
    ),
}


def account_dot(provider_id: ProviderId) -> Text:
    """Render the existing provider-colored account bullet."""
    return Text("●", style=PROVIDER_COLORS.get(provider_id, "dim"))


def cursor_account_dot(
    row: DashboardRow,
    cursor: DashboardCursor,
) -> Text:
    """Render a two-cell cursor prefix before the existing account bullet."""
    marker = Text(
        f"{CURSOR_GLYPH} " if row_is_selected(row, cursor) else "  "
    )
    marker.stylize("bold cyan", 0, 1)
    marker.append_text(account_dot(row.provider_id))
    return marker


def row_marker(
    row: DashboardRow,
    cursor: DashboardCursor,
) -> Text:
    """Render the two-cell cursor prefix followed by the account bullet."""
    marker = Text(
        f"{CURSOR_GLYPH} " if row_is_selected(row, cursor) else "  "
    )
    marker.stylize("bold cyan", 0, 1)
    marker.append_text(account_dot(row.provider_id))
    marker.append(" ")
    return marker


def row_is_selected(
    row: DashboardRow,
    cursor: DashboardCursor,
) -> bool:
    """Return whether one row owns the sole visible cursor."""
    if row.provider_id is not cursor.focused_provider:
        return False
    if isinstance(row, DashboardExternalRow):
        return cursor.external
    return not cursor.external and row.account_id == cursor.account_id


def row_label(row: DashboardRow) -> str:
    """Return local display copy without exposing provider identity."""
    if isinstance(row, DashboardAccount):
        return row.label
    return EXTERNAL_ROW_LABELS[row.provider_id]


def row_plan(row: DashboardRow) -> str:
    """Return a saved plan or suppress it for an external login."""
    return row.plan if isinstance(row, DashboardAccount) else "unknown"


def row_details(
    row: DashboardRow,
    reference_time: datetime,
) -> tuple[str, ...]:
    """Render only actionable or degraded state for one account row."""
    details: list[str] = []
    for state in row.states:
        detail = _state_detail(row, state, reference_time)
        if detail is not None and detail not in details:
            details.append(detail)
    return tuple(details)


def _state_detail(
    row: DashboardRow,
    state: DashboardActionState,
    reference_time: datetime,
) -> str | None:
    if state is DashboardActionState.HEALTHY:
        return None
    if state in {
        DashboardActionState.LOGIN_REQUIRED,
        DashboardActionState.REPAIR_REQUIRED,
    }:
        return _credential_detail(row.provider_id, state)
    if state is DashboardActionState.METRICS_STALE:
        return _metrics_detail(row, reference_time)
    if state in STATIC_STATE_DETAILS:
        return STATIC_STATE_DETAILS[state]
    assert_never(state)


def _credential_detail(
    provider_id: ProviderId,
    state: DashboardActionState,
) -> str:
    provider = PROVIDER_NAMES[provider_id]
    if state is DashboardActionState.LOGIN_REQUIRED:
        return (
            f"Complete the official {provider} login before using this "
            "account."
        )
    return (
        f"{provider} rejected this saved login. Press Enter to repair "
        "and use it."
    )


def _metrics_detail(
    row: DashboardRow,
    reference_time: datetime,
) -> str:
    if not isinstance(row, DashboardAccount):
        raise ValueError("External rows cannot own saved metrics.")
    observed_at = _metrics_observed_at(row)
    if observed_at is None:
        return "Saved metrics are stale; retry scheduled."
    return (
        f"Metrics last updated {_age(observed_at, reference_time)} ago; "
        "retry scheduled."
    )


def _metrics_observed_at(row: DashboardAccount) -> datetime | None:
    observations = tuple(
        observation.observed_at
        for observation in (row.usage, row.activity)
        if observation is not None
    )
    return max(observations, default=None)


def _age(observed_at: datetime, reference_time: datetime) -> str:
    elapsed_minutes = max(
        0,
        int((reference_time - observed_at).total_seconds() // 60),
    )
    hours, minutes = divmod(elapsed_minutes, MINUTES_PER_HOUR)
    days, hours = divmod(hours, HOURS_PER_DAY)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _authentication_failure_copy(
    failure: AuthenticationFailure | RefreshRejectedFailure,
    message_lines: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    if failure.credential_kind is CredentialRecoveryKind.CLAUDE_SETUP_TOKEN:
        command = shlex.join(
            [
                "sidekick-usages",
                "claude",
                "setup-token",
                "--label",
                failure.label,
                "--force",
            ]
        )
        return "authentication failed", (*message_lines, f"Run: {command}")
    if (
        failure.credential_kind
        is CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN
    ):
        command = shlex.join(["sidekick-usages", "refresh", failure.label])
        return (
            "authentication failed",
            (
                *message_lines,
                "Sign in to that Claude account, then run:",
                command,
            ),
        )
    provider_name = PROVIDER_NAMES[failure.provider_id]
    command = shlex.join(["sidekick-usages", "refresh", failure.label])
    return (
        "token expired",
        (
            *message_lines,
            f"Log in to {provider_name} again, then run:",
            command,
        ),
    )


def failure_copy(failure: FetchFailure) -> tuple[str, tuple[str, ...]]:
    """Map one typed application failure to human recovery copy."""
    message_lines = tuple(failure.message.splitlines())
    if isinstance(
        failure,
        AuthenticationFailure | RefreshRejectedFailure,
    ):
        return _authentication_failure_copy(failure, message_lines)
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


def activity_issue_copy(
    provider_id: ProviderId,
    issue: TokenActivityIssue,
) -> tuple[str, ...]:
    """Return safe recovery detail for one account activity issue."""
    if (
        issue.kind is not TokenActivityFailureKind.AUTHENTICATION
        or issue.label is None
    ):
        return ()
    provider_name = PROVIDER_NAMES[provider_id]
    command = shlex.join(["sidekick-usages", "refresh", issue.label])
    return (
        f"Log in to {provider_name} again, then run:",
        command,
    )


def account_activity_issues(
    activity: ProviderTokenActivity | None,
) -> tuple[TokenActivityIssue, ...]:
    """Return only account-scoped issues suitable for warning rows."""
    if not isinstance(
        activity,
        CompleteTokenActivity | PartialTokenActivity | FailedTokenActivity,
    ):
        return ()
    return tuple(issue for issue in activity.issues if issue.label is not None)
