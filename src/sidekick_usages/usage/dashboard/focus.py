"""Provider-verified initial dashboard focus policy."""

from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardProvider,
    DashboardSnapshot,
)


def provider_focus(provider: DashboardProvider) -> DashboardCursor:
    """Return verified-active or first saved-account focus."""
    if not provider.rows:
        raise ValueError("Dashboard focus requires one provider row.")
    if (
        provider.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and provider.active_account_id is not None
    ):
        active = next(
            (
                row
                for row in provider.rows
                if row.account_id == provider.active_account_id
            ),
            None,
        )
        if active is not None:
            return DashboardCursor(
                focused_provider=provider.provider_id,
                account_id=active.account_id,
            )
    return DashboardCursor(
        focused_provider=provider.provider_id,
        account_id=provider.rows[0].account_id,
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
