"""Cursor, row decoration, and actionable detail projection."""

from datetime import datetime

from sidekick_usages.core.accounts.types import MetricsFreshness
from sidekick_usages.core.selection.models import SelectionResult
from sidekick_usages.core.selection.types import (
    ProviderRuntimeState,
    SelectionOutcome,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardCursor,
    DashboardProvider,
    DashboardRow,
)

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
}


def provider_detail(provider: DashboardProvider) -> str | None:
    """Return one provider-scoped runtime advisory."""
    selection_detail = _selection_detail(provider)
    if selection_detail is not None:
        return selection_detail
    runtime_state = provider.status.runtime_state
    if runtime_state is None:
        return None
    if runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE:
        return (
            f"The ambient {PROVIDER_NAMES[provider.provider_id]} authority "
            "is not verified as a saved selection; saved accounts remain "
            "selectable."
        )
    detail = _PROVIDER_STATE_DETAILS.get(runtime_state)
    if detail is None:
        return None
    return f"{PROVIDER_NAMES[provider.provider_id]} {detail}"


def _selection_detail(provider: DashboardProvider) -> str | None:
    """Render active or degraded canonical participant state."""
    status = provider.status.selection
    if isinstance(status, SelectionResult):
        if (
            status.outcome
            is not SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT
        ):
            return None
        target = next(
            (
                row.label
                for row in provider.rows
                if row.account_id == status.target_account_id
            ),
            status.target_account_id,
        )
        unmanaged = _unmanaged_count(provider)
        return (
            f"Selected {target} for epoch {status.epoch.value} · "
            f"{status.outcome.value} · sessions {status.required_count} "
            f"required, {status.ready_count} ready, {status.lost_count} "
            f"lost · unmanaged {unmanaged}."
        )
    if status is None or status.operation_id is None:
        return None
    target = next(
        (
            row.label
            for row in provider.rows
            if row.account_id == status.target_account_id
        ),
        status.target_account_id,
    )
    finalized_epoch = (
        "none"
        if status.finalized_epoch is None
        else str(status.finalized_epoch.value)
    )
    if status.pending_epoch is None or status.phase is None:
        raise AssertionError("Active dashboard selection is incomplete.")
    return (
        f"Selecting {target} for epoch {status.pending_epoch.value} · "
        f"{status.phase.value} · finalized epoch {finalized_epoch} · "
        f"sessions {status.required_count} required, "
        f"{status.ready_count} ready, {status.adopted_count} adopted, "
        f"{status.confirmed_dead_count} lost, "
        f"{status.unreachable_count} unreachable, "
        f"unmanaged {_unmanaged_count(provider)}."
    )


def _unmanaged_count(provider: DashboardProvider) -> str:
    """Render an exact count or admit that no owner supplies one."""
    count = provider.status.unmanaged_sessions
    return "unavailable" if count is None else str(count)


def row_is_selected(
    row: DashboardRow,
    cursor: DashboardCursor,
) -> bool:
    """Return whether one row owns the sole visible cursor."""
    if row.provider_id is not cursor.focused_provider:
        return False
    return row.account_id == cursor.account_id


def row_label(row: DashboardRow) -> str:
    """Return local display copy without exposing provider identity."""
    return row.label


def row_plan(row: DashboardRow) -> str:
    """Return the saved account plan."""
    return row.plan


def row_detail(
    row: DashboardRow,
    reference_time: datetime,
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
    row: DashboardAccount,
    reference_time: datetime,
) -> str | None:
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
