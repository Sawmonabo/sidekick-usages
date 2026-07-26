"""Passive cached dashboard-state composition."""

from datetime import datetime
from typing import assert_never

from sidekick_usages import __version__
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    AccountUsageSnapshot,
    ProviderTokenActivitySnapshot,
)
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.service import (
    ServiceState,
    requires_codex_broker,
)
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.types.service import PackageVersion, ServicePhase
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.snapshots.activity.reader import (
    ActivitySnapshotReader,
)
from sidekick_usages.persistence.snapshots.usage.reader import (
    UsageSnapshotReader,
)
from sidekick_usages.persistence.supervisor.readers.selection import (
    SelectedStateReader,
)
from sidekick_usages.persistence.supervisor.readers.service import (
    ServiceStateReader,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardActivity,
    DashboardExternalRow,
    DashboardProvider,
    DashboardRow,
    DashboardService,
    DashboardSnapshot,
    DashboardUsage,
)

_CURRENT_PACKAGE_VERSION = PackageVersion(__version__)


class CachedDashboardService:
    """Join passive state without opening credential or provider boundaries."""

    def __init__(self, paths: ApplicationPaths) -> None:
        self._accounts = AccountIndexReader(paths.accounts)
        self._usage = UsageSnapshotReader(paths.usage_snapshots)
        self._activity = ActivitySnapshotReader(paths.activity_snapshots)
        self._selected = SelectedStateReader(paths.selected_state)
        self._service = ServiceStateReader(paths.service_state)

    def load(self, reference_time: datetime) -> DashboardSnapshot:
        """Read each cached artifact once and join it by stable account ID."""
        accounts = self._accounts.load()
        usage, usage_conflicts = self._usage.load_all(accounts)
        account_activity, provider_activity = self._activity.load_all(accounts)
        selected = {
            state.provider_id: state for state in self._selected.observe_all()
        }
        service_state = self._service.observe()
        service = self._dashboard_service(service_state)
        usage_by_id = {snapshot.account_id: snapshot for snapshot in usage}
        account_activity_by_id = dict(account_activity)
        provider_activity_by_id = {
            snapshot.provider_id: snapshot for snapshot in provider_activity
        }
        conflict_ids = frozenset(usage_conflicts)
        return DashboardSnapshot(
            providers=tuple(
                self._provider(
                    provider_id,
                    accounts,
                    selected.get(provider_id),
                    self._provider_service_ready(
                        provider_id,
                        accounts,
                        service_state,
                        compatible=service.compatible,
                    ),
                    usage_by_id,
                    account_activity_by_id,
                    provider_activity_by_id.get(provider_id),
                    conflict_ids,
                )
                for provider_id in ProviderId
            ),
            service=service,
            reference_time=reference_time,
        )

    @staticmethod
    def _dashboard_service(state: ServiceState | None) -> DashboardService:
        compatible = (
            state is not None
            and state.protocol_version == PROTOCOL_VERSION
            and state.package_version == _CURRENT_PACKAGE_VERSION
        )
        return DashboardService(
            ready=compatible and state.phase is ServicePhase.READY,
            compatible=compatible,
            phase=None if state is None else state.phase,
            observed_at=None if state is None else state.observed_at,
            failure_code=None if state is None else state.failure_code,
        )

    @staticmethod
    def _provider_service_ready(
        provider_id: ProviderId,
        accounts: tuple[SavedAccount, ...],
        state: ServiceState | None,
        *,
        compatible: bool,
    ) -> bool:
        if state is None or not compatible:
            return False
        broker_required = provider_id is ProviderId.CODEX and any(
            requires_codex_broker(account) for account in accounts
        )
        return state.ready_for(broker_required=broker_required)

    def _provider(
        self,
        provider_id: ProviderId,
        accounts: tuple[SavedAccount, ...],
        selected: SelectedAccountState | None,
        service_ready: bool,
        usage: dict[SidekickAccountId, AccountUsageSnapshot],
        account_activity: dict[
            SidekickAccountId,
            AccountTokenActivitySnapshot,
        ],
        provider_activity: ProviderTokenActivitySnapshot | None,
        usage_conflicts: frozenset[SidekickAccountId],
    ) -> DashboardProvider:
        provider_accounts = tuple(
            account
            for account in accounts
            if account.provider_id is provider_id
        )
        rows: list[DashboardRow] = [
            self._account(
                account,
                selected,
                service_ready,
                usage.get(account.account_id),
                account_activity.get(account.account_id),
                usage_conflicted=account.account_id in usage_conflicts,
            )
            for account in provider_accounts
        ]
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE
        ):
            external_states = [DashboardActionState.EXTERNAL_ACTIVE]
            if not service_ready:
                external_states.append(
                    DashboardActionState.SERVICE_UNAVAILABLE
                )
            rows.append(
                DashboardExternalRow(
                    provider_id=provider_id,
                    observed_at=selected.verified_at,
                    states=tuple(external_states),
                )
            )
        runtime_state = None if selected is None else selected.runtime_state
        provider_available = runtime_state not in {
            ProviderRuntimeState.UNREADABLE,
            ProviderRuntimeState.UNSUPPORTED,
        }
        return DashboardProvider(
            provider_id=provider_id,
            runtime_state=runtime_state,
            active_account_id=(
                selected.account_id
                if selected is not None
                and selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                else None
            ),
            verified_at=None if selected is None else selected.verified_at,
            actions_enabled=service_ready and provider_available,
            rows=tuple(rows),
            activity=(
                None
                if provider_activity is None
                else DashboardActivity(
                    summary=provider_activity.summary,
                    observed_at=provider_activity.fetched_at,
                )
            ),
        )

    def _account(
        self,
        account: SavedAccount,
        selected: SelectedAccountState | None,
        service_ready: bool,
        usage: AccountUsageSnapshot | None,
        activity: AccountTokenActivitySnapshot | None,
        *,
        usage_conflicted: bool,
    ) -> DashboardAccount:
        states = list(self._credential_states(account))
        if usage_conflicted:
            states.append(DashboardActionState.REPAIR_REQUIRED)
        if selected is not None:
            if selected.runtime_state is ProviderRuntimeState.UNREADABLE:
                states.append(DashboardActionState.RECONCILIATION_REQUIRED)
            elif (
                selected.runtime_state is ProviderRuntimeState.UNSUPPORTED
                and account.has_managed_authority
            ):
                states.append(DashboardActionState.PROVIDER_UNSUPPORTED)
        if not service_ready:
            states.append(DashboardActionState.SERVICE_UNAVAILABLE)
        return DashboardAccount(
            account_id=account.account_id,
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            credential_health=account.credential_health,
            active=(
                selected is not None
                and selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                and selected.account_id == account.account_id
            ),
            states=tuple(dict.fromkeys(states)),
            usage=(
                None
                if usage is None
                else DashboardUsage(
                    plan=usage.plan,
                    report=usage.report,
                    observed_at=usage.fetched_at,
                )
            ),
            activity=(
                None
                if activity is None
                else DashboardActivity(
                    summary=activity.summary,
                    observed_at=activity.fetched_at,
                )
            ),
        )

    @staticmethod
    def _credential_states(
        account: SavedAccount,
    ) -> tuple[DashboardActionState, ...]:
        health = account.credential_health
        setup_only = (
            isinstance(account.authority, ClaudeAccountAuthority)
            and account.authority.subscription is None
        )
        if setup_only and health in {
            CredentialHealth.HEALTHY,
            CredentialHealth.UNKNOWN,
        }:
            state = DashboardActionState.SWITCH_SETUP_REQUIRED
        elif not account.has_managed_authority and health in {
            CredentialHealth.HEALTHY,
            CredentialHealth.UNKNOWN,
        }:
            state = DashboardActionState.LOGIN_REQUIRED
        elif health is CredentialHealth.HEALTHY:
            state = DashboardActionState.HEALTHY
        elif health is CredentialHealth.REFRESH_DUE:
            state = (
                DashboardActionState.SETUP_REGENERATION_REQUIRED
                if setup_only
                else DashboardActionState.REPAIR_REQUIRED
            )
        elif health is CredentialHealth.LOGIN_REQUIRED:
            state = (
                DashboardActionState.SETUP_REGENERATION_REQUIRED
                if setup_only
                else DashboardActionState.LOGIN_REQUIRED
            )
        elif health in {
            CredentialHealth.UNREADABLE,
            CredentialHealth.MALFORMED,
        }:
            state = DashboardActionState.REPAIR_REQUIRED
        elif health is CredentialHealth.UNSUPPORTED:
            state = DashboardActionState.PROVIDER_UNSUPPORTED
        elif health is CredentialHealth.RECONCILIATION_REQUIRED:
            state = DashboardActionState.RECONCILIATION_REQUIRED
        elif health is CredentialHealth.UNKNOWN:
            state = None
        else:
            assert_never(health)
        return () if state is None else (state,)
