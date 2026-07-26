"""Provider-verified initial dashboard focus policy."""

from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardCursor,
    DashboardExternalRow,
    DashboardProvider,
    DashboardSnapshot,
)


def provider_focus(provider: DashboardProvider) -> DashboardCursor:
    """Return one provider's verified-active or first-row focus."""
    if not provider.rows:
        raise ValueError("Dashboard focus requires one provider row.")
    if provider.active_account_id is not None:
        active = next(
            (
                row
                for row in provider.rows
                if isinstance(row, DashboardAccount)
                and row.account_id == provider.active_account_id
            ),
            None,
        )
        if active is not None:
            return DashboardCursor(
                focused_provider=provider.provider_id,
                account_id=active.account_id,
            )
    if provider.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE:
        external = next(
            (
                row
                for row in provider.rows
                if isinstance(row, DashboardExternalRow)
            ),
            None,
        )
        if external is not None:
            return DashboardCursor(
                focused_provider=provider.provider_id,
                account_id=None,
                external=True,
            )
    first = provider.rows[0]
    return DashboardCursor(
        focused_provider=provider.provider_id,
        account_id=(
            first.account_id if isinstance(first, DashboardAccount) else None
        ),
        external=isinstance(first, DashboardExternalRow),
    )


def initial_dashboard_cursor(snapshot: DashboardSnapshot) -> DashboardCursor:
    """Prefer Claude, then the first provider with a displayed row."""
    provider = next(
        (
            candidate
            for candidate in snapshot.providers
            if candidate.provider_id is ProviderId.CLAUDE and candidate.rows
        ),
        None,
    )
    if provider is None:
        provider = next(
            (candidate for candidate in snapshot.providers if candidate.rows),
            None,
        )
    return (
        DashboardCursor(
            focused_provider=None,
            account_id=None,
        )
        if provider is None
        else provider_focus(provider)
    )
