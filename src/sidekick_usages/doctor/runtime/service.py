"""Project cached dashboard truth into doctor runtime diagnostics."""

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.doctor.runtime.models import (
    AccountRuntimeDiagnostic,
)
from sidekick_usages.doctor.runtime.types import (
    NativeAccountRelation,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardProvider,
    DashboardSnapshot,
)

_NATIVE_RELATIONS = {
    ProviderRuntimeState.SAVED_ACTIVE: NativeAccountRelation.INACTIVE,
    ProviderRuntimeState.EXTERNAL_ACTIVE: NativeAccountRelation.EXTERNAL,
    ProviderRuntimeState.LOGGED_OUT: NativeAccountRelation.LOGGED_OUT,
    ProviderRuntimeState.UNREADABLE: (
        NativeAccountRelation.RECONCILIATION_REQUIRED
    ),
    ProviderRuntimeState.UNSUPPORTED: NativeAccountRelation.UNSUPPORTED,
}


class DoctorRuntimeService:
    """Provide one cached runtime diagnostic per saved account."""

    def __init__(
        self,
        accounts: tuple[SavedAccount, ...],
        snapshot: DashboardSnapshot | None,
    ) -> None:
        self._diagnostics = (
            _unobserved(accounts)
            if snapshot is None
            else _snapshot_diagnostics(snapshot)
        )
        expected = {account.account_id for account in accounts}
        if set(self._diagnostics) != expected:
            raise ValueError("Doctor runtime accounts do not match.")

    def diagnostic(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRuntimeDiagnostic:
        """Return the exact diagnostic for one saved account."""
        return self._diagnostics[account_id]


def _unobserved(
    accounts: tuple[SavedAccount, ...],
) -> dict[SidekickAccountId, AccountRuntimeDiagnostic]:
    return {
        account.account_id: AccountRuntimeDiagnostic(
            account_id=account.account_id,
            native_relation=NativeAccountRelation.UNKNOWN,
            metrics_freshness=MetricsFreshness.UNAVAILABLE,
            metrics_observed_at=None,
        )
        for account in accounts
    }


def _snapshot_diagnostics(
    snapshot: DashboardSnapshot,
) -> dict[SidekickAccountId, AccountRuntimeDiagnostic]:
    return {
        row.account_id: _diagnostic(row, provider)
        for provider in snapshot.providers
        for row in provider.rows
        if isinstance(row, DashboardAccount)
    }


def _diagnostic(
    account: DashboardAccount,
    provider: DashboardProvider,
) -> AccountRuntimeDiagnostic:
    observations = tuple(
        observation.observed_at
        for observation in (account.usage, account.activity)
        if observation is not None
    )
    return AccountRuntimeDiagnostic(
        account_id=account.account_id,
        native_relation=_native_relation(account, provider),
        metrics_freshness=(
            MetricsFreshness.STALE
            if observations
            else MetricsFreshness.UNAVAILABLE
        ),
        metrics_observed_at=max(observations, default=None),
    )


def _native_relation(
    account: DashboardAccount,
    provider: DashboardProvider,
) -> NativeAccountRelation:
    if account.active:
        return NativeAccountRelation.ACTIVE
    state = provider.runtime_state
    if state is None:
        return NativeAccountRelation.UNKNOWN
    return _NATIVE_RELATIONS.get(
        state,
        NativeAccountRelation.UNKNOWN,
    )
