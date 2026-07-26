"""Project cached dashboard truth into doctor runtime diagnostics."""

from datetime import datetime

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    CodexAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
    ProviderRuntimeState,
)
from sidekick_usages.doctor.runtime.models import (
    AccountRuntimeDiagnostic,
    ScheduledOperationDiagnostic,
    UnfinishedActivationDiagnostic,
)
from sidekick_usages.doctor.runtime.types import (
    NativeAccountRelation,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_generation_relation,
)
from sidekick_usages.providers.codex.generation import (
    codex_generation_relation,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
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
        selected_states: tuple[SelectedAccountState, ...],
        operations: tuple[DueOperation, ...],
        activations: tuple[ActivationRecord, ...],
    ) -> None:
        metrics = (
            _unobserved_metrics(accounts)
            if snapshot is None
            else _snapshot_metrics(snapshot)
        )
        expected = {account.account_id for account in accounts}
        if set(metrics) != expected:
            raise ValueError("Doctor runtime accounts do not match.")
        account_map = {
            account.account_id: account for account in accounts
        }
        for state in selected_states:
            if state.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE:
                continue
            selected_account_id = state.account_id
            if selected_account_id is None:
                raise ValueError(
                    "Doctor selected account identity is incomplete."
                )
            selected_account = account_map.get(selected_account_id)
            if selected_account is None:
                raise ValueError(
                    "Doctor selected account does not exist."
                )
            if selected_account.provider_id is not state.provider_id:
                raise ValueError(
                    "Doctor selected provider does not match its account."
                )
            if (
                selected_account.provider_identity
                != state.provider_identity
            ):
                raise ValueError(
                    "Doctor selected identity does not match its account."
                )
        selected = {state.provider_id: state for state in selected_states}
        self._diagnostics = {
            account.account_id: _diagnostic(
                account,
                selected.get(account.provider_id),
                metrics[account.account_id],
            )
            for account in accounts
        }
        self.operations = tuple(
            _operation_diagnostic(operation, account_map)
            for operation in operations
        )
        self.unfinished_activations = tuple(
            _activation_diagnostic(activation, account_map)
            for activation in activations
        )

    def diagnostic(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRuntimeDiagnostic:
        """Return the exact diagnostic for one saved account."""
        return self._diagnostics[account_id]


def _unobserved_metrics(
    accounts: tuple[SavedAccount, ...],
) -> dict[SidekickAccountId, tuple[MetricsFreshness, datetime | None]]:
    return {
        account.account_id: (MetricsFreshness.UNAVAILABLE, None)
        for account in accounts
    }


def _operation_diagnostic(
    operation: DueOperation,
    accounts: dict[SidekickAccountId, SavedAccount],
) -> ScheduledOperationDiagnostic:
    account = (
        None
        if operation.account_id is None
        else accounts.get(operation.account_id)
    )
    if operation.account_id is not None and account is None:
        raise ValueError("Doctor operation account does not exist.")
    if (
        account is not None
        and account.provider_id is not operation.provider_id
    ):
        raise ValueError(
            "Doctor operation provider does not match its account."
        )
    return ScheduledOperationDiagnostic(
        provider_id=operation.provider_id,
        account_label=None if account is None else account.label,
        kind=operation.kind,
        state=operation.state,
        due_at=operation.due_at,
        updated_at=operation.updated_at,
        attempts=operation.attempts,
        failure_code=operation.failure_code,
    )


def _activation_diagnostic(
    activation: ActivationRecord,
    accounts: dict[SidekickAccountId, SavedAccount],
) -> UnfinishedActivationDiagnostic:
    account = accounts.get(activation.target_account_id)
    if account is None:
        raise ValueError("Doctor activation target does not exist.")
    if account.provider_id is not activation.provider_id:
        raise ValueError(
            "Doctor activation provider does not match its target."
        )
    return UnfinishedActivationDiagnostic(
        provider_id=activation.provider_id,
        target_label=account.label,
        phase=activation.phase,
        started_at=activation.started_at,
        updated_at=activation.updated_at,
        failure_code=activation.failure_code,
    )


def _snapshot_metrics(
    snapshot: DashboardSnapshot,
) -> dict[SidekickAccountId, tuple[MetricsFreshness, datetime | None]]:
    return {
        row.account_id: _metric_observation(row)
        for provider in snapshot.providers
        for row in provider.rows
        if isinstance(row, DashboardAccount)
    }


def _diagnostic(
    account: SavedAccount,
    selected: SelectedAccountState | None,
    metrics: tuple[MetricsFreshness, datetime | None],
) -> AccountRuntimeDiagnostic:
    metrics_freshness, metrics_observed_at = metrics
    return AccountRuntimeDiagnostic(
        account_id=account.account_id,
        native_relation=_native_relation(account, selected),
        selected_generation_relation=_generation_relation(account, selected),
        metrics_freshness=metrics_freshness,
        metrics_observed_at=metrics_observed_at,
    )


def _metric_observation(
    account: DashboardAccount,
) -> tuple[MetricsFreshness, datetime | None]:
    observations = tuple(
        observation.observed_at
        for observation in (account.usage, account.activity)
        if observation is not None
    )
    observed_at = max(observations, default=None)
    return (
        (
            MetricsFreshness.STALE
            if observations
            else MetricsFreshness.UNAVAILABLE
        ),
        observed_at,
    )


def _native_relation(
    account: SavedAccount,
    selected: SelectedAccountState | None,
) -> NativeAccountRelation:
    if selected is None:
        return NativeAccountRelation.UNKNOWN
    if (
        selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and selected.account_id == account.account_id
    ):
        return NativeAccountRelation.ACTIVE
    return _NATIVE_RELATIONS.get(
        selected.runtime_state,
        NativeAccountRelation.UNKNOWN,
    )


def _generation_relation(
    account: SavedAccount,
    selected: SelectedAccountState | None,
) -> AuthorityGenerationRelation:
    saved = _authority_generation(account)
    if (
        selected is None
        or selected.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
        or selected.account_id != account.account_id
        or selected.runtime_generation is None
        or saved is None
    ):
        return AuthorityGenerationRelation.NOT_SAFELY_COMPARABLE
    if isinstance(account.authority, ClaudeAccountAuthority):
        return claude_generation_relation(saved, selected.runtime_generation)
    try:
        return codex_generation_relation(saved, selected.runtime_generation)
    except ValueError:
        return AuthorityGenerationRelation.NOT_SAFELY_COMPARABLE


def _authority_generation(
    account: SavedAccount,
) -> AuthorityGeneration | None:
    authority = account.authority
    if isinstance(authority, ClaudeAccountAuthority):
        subscription = authority.subscription
        return (
            subscription.generation
            if isinstance(subscription, ClaudeManagedLoginAuthority)
            else None
        )
    if isinstance(authority, CodexAccountAuthority):
        return authority.subscription.generation
    raise AssertionError("Saved account authority is unsupported.")
