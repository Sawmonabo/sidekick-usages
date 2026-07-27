"""Cursor, row decoration, and actionable detail projection."""

from datetime import datetime

from sidekick_usages.core.accounts.types import MetricsFreshness
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardCursor,
    DashboardExternalRow,
    DashboardProvider,
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
_PROVIDER_STATE_DETAILS = {
    ProviderRuntimeState.UNREADABLE: (
        "login could not be verified; account switching is paused."
    ),
    ProviderRuntimeState.UNSUPPORTED: (
        "account verification is unavailable; saved metrics remain visible."
    ),
}
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
FAILURE_STATE_DETAILS = {
    DashboardActionState.SETUP_REGENERATION_REQUIRED: (
        "Generate a new Claude setup token before using this account."
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
EXTERNAL_LOGIN_DETAIL = "This external login is not saved in Sidekick."
SWITCH_SETUP_DETAIL = "Enter to connect this account for Claude switching."


def provider_detail(provider: DashboardProvider) -> str | None:
    """Return one provider-scoped runtime advisory."""
    if provider.runtime_state is None:
        return None
    detail = _PROVIDER_STATE_DETAILS.get(provider.runtime_state)
    if detail is None:
        return None
    return f"{PROVIDER_NAMES[provider.provider_id]} {detail}"


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


def row_detail(
    row: DashboardRow,
    cursor: DashboardCursor,
    reference_time: datetime,
    *,
    actions_enabled: bool,
) -> str | None:
    """Return the highest-priority actionable or degraded row detail."""
    for state in row.states:
        if state in {
            DashboardActionState.LOGIN_REQUIRED,
            DashboardActionState.REPAIR_REQUIRED,
        }:
            return _credential_detail(row.provider_id, state)
        if state in FAILURE_STATE_DETAILS:
            return FAILURE_STATE_DETAILS[state]
    if (
        DashboardActionState.SWITCH_SETUP_REQUIRED in row.states
        and actions_enabled
        and row_is_selected(row, cursor)
    ):
        return SWITCH_SETUP_DETAIL
    if DashboardActionState.EXTERNAL_ACTIVE in row.states:
        return EXTERNAL_LOGIN_DETAIL
    return _metrics_detail(row, reference_time)


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
) -> str | None:
    if not isinstance(row, DashboardAccount):
        return None
    if row.metrics_freshness is MetricsFreshness.UNAVAILABLE:
        return "Live metrics refresh failed; no saved metrics available."
    if row.metrics_freshness is not MetricsFreshness.STALE:
        return None
    observed_at = _metrics_observed_at(row)
    if observed_at is None:
        raise ValueError("Stale dashboard metrics require an observation.")
    return (
        "Live metrics refresh failed; showing data from "
        f"{_age(observed_at, reference_time)} ago."
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
