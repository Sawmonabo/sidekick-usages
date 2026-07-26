"""Cursor, row decoration, and actionable detail projection."""

from datetime import datetime
from typing import assert_never

from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardCursor,
    DashboardExternalRow,
    DashboardRow,
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
STATIC_STATE_DETAILS = {
    DashboardActionState.SETUP_REGENERATION_REQUIRED: (
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
