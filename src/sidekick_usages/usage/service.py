"""Provider-neutral concurrent usage orchestration."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.models import AccountUsageSnapshot
from sidekick_usages.core.types import ProviderId
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import (
    CredentialRefresher,
    RefreshOutcome,
    TokenMaintenanceService,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.usage.activity import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
    TokenActivityCollector,
)
from sidekick_usages.usage.failures import (
    credential_recovery_kind,
    failure_from_error,
    persistence_failure,
)
from sidekick_usages.usage.lookup.models import (
    AccountLookupCompletion,
    AccountLookupObserver,
    AccountLookupReading,
    AccountMutationIntent,
    AccountMutationResult,
    AccountRefreshResult,
    CredentialRefreshIntent,
    CurrentUsageReading,
    LocalActivityReading,
    ProviderStateIntent,
    ProviderStateResult,
)
from sidekick_usages.usage.lookup.service import (
    AccountCredentialAccess,
    AccountLookupService,
)
from sidekick_usages.usage.lookup.wave import UsageLookupWave
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    RefreshRejectedFailure,
    UsageCheckResult,
)
from sidekick_usages.usage.ports import UsagePersistence


class UsageCheckService:
    """Select accounts and return deterministic concurrent check outcomes."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[ProviderId, Provider],
        credentials: CredentialRefresher | None,
        *,
        clock: Clock,
        credential_access: AccountCredentialAccess,
        local_activity_sources: Mapping[
            ProviderId,
            LocalTokenActivitySource,
        ]
        | None = None,
        account_activity_sources: Mapping[
            ProviderId,
            AccountTokenActivitySource,
        ]
        | None = None,
        persistence: UsagePersistence | None = None,
    ) -> None:
        """Bind checking to its invocation-scoped dependencies."""
        self._store = store
        self._clock = clock
        self._usage_snapshots = (
            None if persistence is None else persistence.usage
        )
        self._maintenance = (
            None
            if credentials is None
            else TokenMaintenanceService(
                store,
                credentials,
                clock=clock,
            )
        )
        activity = TokenActivityCollector(
            http,
            {} if local_activity_sources is None else local_activity_sources,
            (
                {}
                if account_activity_sources is None
                else account_activity_sources
            ),
            None if persistence is None else persistence.activity,
        )
        lookup = AccountLookupService(
            http,
            providers,
            activity,
            credential_access,
        )
        self._activity = activity
        self._wave = UsageLookupWave(lookup, activity)

    def check(
        self,
        provider_id: ProviderId | None = None,
        *,
        observe: AccountLookupObserver | None = None,
    ) -> UsageCheckResult:
        """Check every selected account in one bounded concurrent wave.

        :param provider_id: Optional provider filter.
        :param observe: Optional owner-thread account completion observer.
        :returns: Immutable outcomes in saved-account order.
        """
        return self._check(
            self._store.saved_accounts(provider_id),
            observe=observe,
        )

    def check_account(
        self,
        account_id: SidekickAccountId,
    ) -> UsageCheckResult:
        """Check one exact stable account for an isolated worker."""
        account = self._store.read_saved(account_id)
        if account is None:
            raise SourceChangedError
        return self._check((account,))

    def _check(
        self,
        accounts: tuple[SavedAccount, ...],
        *,
        observe: AccountLookupObserver | None = None,
    ) -> UsageCheckResult:
        """Check one stable account population in its supplied order."""
        reference_time = self._clock.now()
        readings: dict[
            SidekickAccountId,
            AccountLookupReading,
        ] = {}
        local_readings: dict[ProviderId, LocalActivityReading] = {}
        local_providers = self._activity.local_providers(accounts)
        for reading in self._wave.run(
            accounts,
            local_providers,
            reference_time,
            self._mutate,
        ):
            if isinstance(reading, LocalActivityReading):
                local_readings[reading.provider_id] = reading
                continue
            account = accounts[reading.ordinal]
            if (
                account.account_id != reading.account_id
                or account.provider_id is not reading.provider_id
            ):
                raise SourceChangedError
            readings[account.account_id] = reading
            if observe is not None:
                observe(
                    self._preview_completion(
                        account,
                        reading,
                        reference_time,
                    )
                )
        completions, activity_allowed = self._complete_lookups(
            accounts,
            readings,
            reference_time,
        )
        contributions, local_readings = self._activity.complete(
            accounts,
            {
                account_id: reading.activity
                for account_id, reading in readings.items()
            },
            activity_allowed,
            local_readings,
            reference_time,
        )
        ordered = tuple(
            completions[account.account_id] for account in accounts
        )
        return UsageCheckResult(
            tuple(
                completion.usage
                for completion in ordered
                if completion.usage is not None
            ),
            tuple(
                completion.failure
                for completion in ordered
                if completion.failure is not None
            ),
            reference_time,
            self._activity.aggregate(
                accounts,
                contributions,
                local_readings,
            ),
        )

    def _mutate(
        self,
        intent: AccountMutationIntent,
    ) -> AccountMutationResult:
        """Apply one secret-free lookup intent on the owner thread."""
        if isinstance(intent, CredentialRefreshIntent):
            refreshed = self._refresh(intent)
            return (
                AccountRefreshResult(account=refreshed)
                if isinstance(refreshed, SavedAccount)
                else AccountRefreshResult(failure=refreshed)
            )
        if not isinstance(intent, ProviderStateIntent):
            raise TypeError("Unknown account lookup mutation intent.")
        try:
            self._store.persist_state(
                replace(intent.account, plan=intent.plan),
                expected=intent.account,
            )
        except PersistenceError as error:
            return ProviderStateResult(
                failure=persistence_failure(intent.account, error)
            )
        return ProviderStateResult()

    def _refresh(
        self,
        intent: CredentialRefreshIntent,
    ) -> SavedAccount | FetchFailure:
        """Refresh one requested account on the serialized owner thread."""
        account = intent.account
        maintenance = self._maintenance
        if maintenance is None:
            return RefreshRejectedFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message="Credential refresh requires its managed authority.",
                credential_kind=credential_recovery_kind(account),
            )
        try:
            outcome = maintenance.refresh_account(
                account,
                force=True,
                reason=intent.reason,
            )
        except PersistenceError as error:
            return persistence_failure(account, error)
        return self._refresh_outcome(account, outcome)

    def _refresh_outcome(
        self,
        account: SavedAccount,
        outcome: RefreshOutcome,
    ) -> SavedAccount | FetchFailure:
        """Translate one owner-thread refresh into current metadata."""
        if outcome.refreshed:
            refreshed = self._store.read_saved(account.account_id)
            if refreshed is None:
                return persistence_failure(
                    account,
                    SourceChangedError(),
                )
            return refreshed
        if outcome.persistence_error is not None:
            return persistence_failure(
                account,
                outcome.persistence_error,
            )
        if outcome.operational_error is not None:
            return failure_from_error(
                account,
                outcome.operational_error,
            )
        return RefreshRejectedFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=outcome.message,
            credential_kind=credential_recovery_kind(account),
            provider_failure=outcome.provider_failure,
        )

    def _complete_lookups(
        self,
        accounts: tuple[SavedAccount, ...],
        readings: Mapping[SidekickAccountId, AccountLookupReading],
        reference_time: datetime,
    ) -> tuple[
        dict[SidekickAccountId, AccountLookupCompletion],
        dict[SidekickAccountId, bool],
    ]:
        """Finalize one lookup wave through one usage snapshot batch."""
        pending = {
            account.account_id: self._usage_snapshot(
                account,
                readings[account.account_id].usage,
                reference_time,
            )
            for account in accounts
            if readings[account.account_id].usage is not None
        }
        current, persistence_failures = self._save_current_usage(
            accounts,
            pending,
        )
        retained_accounts = tuple(
            account
            for account in accounts
            if account.account_id not in current
        )
        retained = self._load_retained_usage(retained_accounts)
        completions: dict[
            SidekickAccountId,
            AccountLookupCompletion,
        ] = {}
        activity_allowed: dict[SidekickAccountId, bool] = {}
        for account in accounts:
            account_id = account.account_id
            reading = readings[account_id]
            snapshot = current.get(account_id)
            failure = persistence_failures.get(account_id, reading.failure)
            if snapshot is not None:
                usage = self._account_usage(
                    account,
                    snapshot,
                    MetricsFreshness.FRESH,
                )
                failure = None
            else:
                usage = self._retained_usage(
                    account,
                    retained.get(account_id),
                )
            if usage is None and failure is None:
                raise AssertionError("Account lookup outcome disappeared.")
            completions[account_id] = self._completion(
                reading,
                usage=usage,
                failure=failure,
            )
            activity_allowed[account_id] = (
                reading.activity_eligible
                and account_id not in persistence_failures
            )
        return completions, activity_allowed

    def _save_current_usage(
        self,
        accounts: tuple[SavedAccount, ...],
        pending: Mapping[SidekickAccountId, AccountUsageSnapshot],
    ) -> tuple[
        dict[SidekickAccountId, AccountUsageSnapshot],
        dict[SidekickAccountId, FetchFailure],
    ]:
        """Commit all current usage through at most one artifact write."""
        if self._usage_snapshots is None:
            return dict(pending), {}
        if not pending:
            return {}, {}
        account_ids = tuple(pending)
        try:
            durable = self._usage_snapshots.save_many(tuple(pending.values()))
        except PersistenceError as error:
            account_by_id = {
                account.account_id: account for account in accounts
            }
            return (
                {},
                {
                    account_id: persistence_failure(
                        account_by_id[account_id],
                        error,
                    )
                    for account_id in account_ids
                },
            )
        return (
            dict(zip(account_ids, durable, strict=True)),
            {},
        )

    def _load_retained_usage(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountUsageSnapshot]:
        """Load retained usage through at most one artifact decode."""
        if self._usage_snapshots is None or not accounts:
            return {}
        try:
            return self._usage_snapshots.load_many(accounts)
        except PersistenceError:
            return {}

    @staticmethod
    def _usage_snapshot(
        account: SavedAccount,
        reading: CurrentUsageReading | None,
        reference_time: datetime,
    ) -> AccountUsageSnapshot:
        if reading is None:
            raise AssertionError("Current usage reading disappeared.")
        return AccountUsageSnapshot(
            account_id=account.account_id,
            provider_id=account.provider_id,
            provider_identity=account.provider_identity,
            plan=reading.plan,
            report=reading.report,
            fetched_at=reference_time,
        )

    @classmethod
    def _retained_usage(
        cls,
        account: SavedAccount,
        snapshot: AccountUsageSnapshot | None,
    ) -> AccountUsage | None:
        """Project one retained usage snapshot after a failed fetch."""
        if snapshot is None:
            return None
        return cls._account_usage(
            account,
            snapshot,
            MetricsFreshness.STALE,
        )

    @classmethod
    def _preview_completion(
        cls,
        account: SavedAccount,
        reading: AccountLookupReading,
        reference_time: datetime,
    ) -> AccountLookupCompletion:
        """Publish one immutable in-memory reading before batch commit."""
        current = reading.usage
        usage = (
            None
            if current is None
            else cls._account_usage(
                account,
                cls._usage_snapshot(account, current, reference_time),
                MetricsFreshness.FRESH,
            )
        )
        return cls._completion(
            reading,
            usage=usage,
            failure=reading.failure,
        )

    @staticmethod
    def _completion(
        reading: AccountLookupReading,
        *,
        usage: AccountUsage | None,
        failure: FetchFailure | None,
    ) -> AccountLookupCompletion:
        return AccountLookupCompletion(
            ordinal=reading.ordinal,
            account_id=reading.account_id,
            label=reading.label,
            provider_id=reading.provider_id,
            usage=usage,
            failure=failure,
        )

    @staticmethod
    def _account_usage(
        account: SavedAccount,
        snapshot: AccountUsageSnapshot,
        freshness: MetricsFreshness,
    ) -> AccountUsage:
        """Project one stable snapshot through current account metadata."""
        return AccountUsage(
            account_id=account.account_id,
            label=account.label,
            provider_id=account.provider_id,
            plan=snapshot.plan,
            report=snapshot.report,
            fetched_at=snapshot.fetched_at,
            freshness=freshness,
        )
