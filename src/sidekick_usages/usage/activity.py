"""Application policy for scoped provider token activity."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    TokenActivityReading,
    TokenActivitySummary,
    TokenActivityUnavailable,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.credentials.authorities import CredentialLease
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    InvalidPayloadError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.errors import ActivitySnapshotError
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.usage.lookup.models import (
    AccountActivityContribution,
    ActivityObservation,
    LocalActivityReading,
)
from sidekick_usages.usage.models import (
    CompleteTokenActivity,
    FailedTokenActivity,
    PartialTokenActivity,
    ProviderTokenActivity,
    TokenActivityFailureKind,
    TokenActivityIssue,
    UnavailableTokenActivity,
)
from sidekick_usages.usage.ports import AccountTokenActivitySnapshots

_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807


class LocalTokenActivitySource(Protocol):
    """Read one provider's local-installation token activity."""

    provider_id: ProviderId

    def read(self, reference_time: datetime) -> TokenActivityReading:
        """Return one local-installation activity reading."""


class AccountTokenActivitySource(Protocol):
    """Read one saved account's authoritative token activity."""

    provider_id: ProviderId

    def read(
        self,
        account: AuthenticatedAccount[CredentialLease],
        http: HttpClient,
    ) -> TokenActivityReading:
        """Return one account-scoped activity reading."""


class TokenActivityCollector:
    """Read and aggregate activity around owner-thread persistence."""

    def __init__(
        self,
        http: HttpClient,
        local_sources: Mapping[ProviderId, LocalTokenActivitySource],
        account_sources: Mapping[ProviderId, AccountTokenActivitySource],
        snapshots: AccountTokenActivitySnapshots | None = None,
    ) -> None:
        """Bind collection to validated provider source mappings."""
        self._http = http
        self._local_sources = dict(local_sources)
        self._account_sources = dict(account_sources)
        self._snapshots = snapshots
        if any(
            provider_id is not source.provider_id
            for provider_id, source in (
                *self._local_sources.items(),
                *self._account_sources.items(),
            )
        ):
            raise ValueError(
                "Token activity source keys must match their provider ids."
            )
        if not self._local_sources.keys().isdisjoint(self._account_sources):
            raise ValueError(
                "A provider cannot have local and account activity sources."
            )

    def local_providers(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> tuple[ProviderId, ...]:
        """Return selected local sources in deterministic provider order."""
        provider_order = tuple(
            dict.fromkeys(account.provider_id for account in accounts)
        )
        return tuple(
            provider_id
            for provider_id in provider_order
            if provider_id in self._local_sources
        )

    def read_local(
        self,
        provider_id: ProviderId,
        reference_time: datetime,
    ) -> LocalActivityReading:
        """Read one provider-local source inside the concurrent wave."""
        source = self._local_sources.get(provider_id)
        if source is None:
            raise ValueError("Provider has no local activity source.")
        try:
            reading = source.read(reference_time)
        except UsageError as error:
            observation: ActivityObservation = self._issue(error)
        else:
            observation = (
                reading
                if reading.scope is TokenActivityScope.LOCAL_INSTALLATION
                else self._scope_issue()
            )
        return LocalActivityReading(
            provider_id=provider_id,
            observation=observation,
        )

    def read_account(
        self,
        account: AuthenticatedAccount[CredentialLease],
    ) -> ActivityObservation | None:
        """Read account activity through its already-active usage lease."""
        source = self._account_sources.get(account.account.provider_id)
        if source is None:
            return None
        try:
            reading = source.read(account, self._http)
        except UsageError as error:
            return self._issue(error, account.account.label)
        if reading.scope is not TokenActivityScope.ACCOUNT:
            return self._scope_issue(account.account.label)
        return reading

    def complete_accounts(
        self,
        accounts: tuple[SavedAccount, ...],
        observations: Mapping[
            SidekickAccountId,
            ActivityObservation | None,
        ],
        fetch_allowed: Mapping[SidekickAccountId, bool],
        reference_time: datetime,
    ) -> dict[SidekickAccountId, AccountActivityContribution]:
        """Finalize account observations through one snapshot batch."""
        selected = tuple(
            account
            for account in accounts
            if account.provider_id in self._account_sources
        )
        summaries: dict[SidekickAccountId, TokenActivitySummary] = {}
        issues = {account.account_id: [] for account in selected}
        labels = {
            account.account_id: account.label for account in selected
        }
        pending: dict[
            SidekickAccountId,
            AccountTokenActivitySnapshot,
        ] = {}
        for account in selected:
            account_id = account.account_id
            observation = observations.get(account_id)
            if not fetch_allowed.get(account_id, False):
                continue
            if isinstance(observation, TokenActivityIssue):
                issues[account_id].append(observation)
            elif isinstance(observation, TokenActivitySummary):
                summaries[account_id] = observation
                snapshot = self._activity_snapshot(
                    account,
                    observation,
                    reference_time,
                )
                if snapshot is not None:
                    pending[account_id] = snapshot
        self._save_current(pending, summaries, issues, labels)
        missing = tuple(
            account
            for account in selected
            if account.account_id not in summaries
        )
        self._load_retained(missing, summaries, issues)
        return {
            account.account_id: AccountActivityContribution(
                account_id=account.account_id,
                summary=summaries.get(account.account_id),
                issues=tuple(issues[account.account_id]),
            )
            for account in selected
        }

    def aggregate(
        self,
        accounts: tuple[SavedAccount, ...],
        contributions: Mapping[
            SidekickAccountId,
            AccountActivityContribution,
        ],
        local_readings: Mapping[ProviderId, LocalActivityReading],
    ) -> tuple[ProviderTokenActivity, ...]:
        """Aggregate completed readings in deterministic provider order."""
        provider_order = tuple(
            dict.fromkeys(account.provider_id for account in accounts)
        )
        outcomes: list[ProviderTokenActivity] = []
        for provider_id in provider_order:
            selected = tuple(
                account
                for account in accounts
                if account.provider_id is provider_id
            )
            if provider_id in self._local_sources:
                outcomes.append(
                    self._local_outcome(local_readings[provider_id])
                )
            elif provider_id in self._account_sources:
                outcomes.append(
                    self._account_outcome(
                        provider_id,
                        selected,
                        contributions,
                    )
                )
        return tuple(outcomes)

    @classmethod
    def _local_outcome(
        cls,
        reading: LocalActivityReading,
    ) -> ProviderTokenActivity:
        observation = reading.observation
        if isinstance(observation, TokenActivityIssue):
            return FailedTokenActivity(
                provider_id=reading.provider_id,
                scope=TokenActivityScope.LOCAL_INSTALLATION,
                issues=(observation,),
            )
        if isinstance(observation, TokenActivityUnavailable):
            return UnavailableTokenActivity(
                provider_id=reading.provider_id,
                scope=TokenActivityScope.LOCAL_INSTALLATION,
            )
        return CompleteTokenActivity(
            provider_id=reading.provider_id,
            summary=observation,
        )

    @classmethod
    def _account_outcome(
        cls,
        provider_id: ProviderId,
        selected: tuple[SavedAccount, ...],
        contributions: Mapping[
            SidekickAccountId,
            AccountActivityContribution,
        ],
    ) -> ProviderTokenActivity:
        total = 0
        since_dates: list[date] = []
        has_unknown_since = False
        covered = 0
        issues: list[TokenActivityIssue] = []
        for account in selected:
            contribution = contributions.get(account.account_id)
            if contribution is None:
                continue
            issues.extend(contribution.issues)
            summary = contribution.summary
            if summary is None:
                continue
            if summary.total_tokens > _MAX_TOKEN_COUNT - total:
                issues.append(
                    TokenActivityIssue(
                        kind=TokenActivityFailureKind.SOURCE_MALFORMED,
                        message=(
                            "Provider token activity total exceeds its "
                            "boundary."
                        ),
                        label=account.label,
                    )
                )
                continue
            total += summary.total_tokens
            covered += 1
            if summary.since is None:
                has_unknown_since = True
            else:
                since_dates.append(summary.since)
        if covered == 0:
            if issues:
                return FailedTokenActivity(
                    provider_id=provider_id,
                    scope=TokenActivityScope.ACCOUNT,
                    issues=tuple(issues),
                )
            return UnavailableTokenActivity(
                provider_id=provider_id,
                scope=TokenActivityScope.ACCOUNT,
            )
        summary = TokenActivitySummary(
            total_tokens=total,
            scope=TokenActivityScope.ACCOUNT,
            since=(
                None
                if has_unknown_since or not since_dates
                else min(since_dates)
            ),
        )
        if covered == len(selected):
            return CompleteTokenActivity(
                provider_id=provider_id,
                summary=summary,
                issues=tuple(issues),
            )
        return PartialTokenActivity(
            provider_id=provider_id,
            summary=summary,
            covered_accounts=covered,
            saved_accounts=len(selected),
            issues=tuple(issues),
        )

    def _load_retained(
        self,
        accounts: tuple[SavedAccount, ...],
        summaries: dict[SidekickAccountId, TokenActivitySummary],
        issues: dict[SidekickAccountId, list[TokenActivityIssue]],
    ) -> None:
        if self._snapshots is None or not accounts:
            return
        try:
            retained = self._snapshots.load_many(accounts)
        except ActivitySnapshotError as error:
            for account in accounts:
                issues[account.account_id].append(
                    self._issue(error, account.label)
                )
            return
        for account_id, snapshot in retained.items():
            summaries[account_id] = snapshot.summary

    def _save_current(
        self,
        pending: Mapping[
            SidekickAccountId,
            AccountTokenActivitySnapshot,
        ],
        summaries: dict[SidekickAccountId, TokenActivitySummary],
        issues: dict[SidekickAccountId, list[TokenActivityIssue]],
        labels: Mapping[SidekickAccountId, AccountLabel],
    ) -> None:
        if self._snapshots is None or not pending:
            return
        account_ids = tuple(pending)
        try:
            durable = self._snapshots.save_many(tuple(pending.values()))
        except ActivitySnapshotError as error:
            for account_id in account_ids:
                issues[account_id].append(
                    self._issue(error, labels[account_id])
                )
            return
        for account_id, snapshot in zip(account_ids, durable, strict=True):
            summaries[account_id] = snapshot.summary

    def _activity_snapshot(
        self,
        account: SavedAccount,
        summary: TokenActivitySummary,
        reference_time: datetime,
    ) -> AccountTokenActivitySnapshot | None:
        provider_identity = account.provider_identity
        if self._snapshots is None or provider_identity is None:
            return None
        return AccountTokenActivitySnapshot(
            provider_id=account.provider_id,
            provider_account_id=str(provider_identity),
            summary=summary,
            fetched_at=reference_time,
        )

    @staticmethod
    def _scope_issue(
        label: AccountLabel | None = None,
    ) -> TokenActivityIssue:
        return TokenActivityIssue(
            kind=TokenActivityFailureKind.PROVIDER,
            message="Provider token activity returned an invalid scope.",
            label=label,
        )

    @staticmethod
    def _issue(
        error: UsageError,
        label: AccountLabel | None = None,
    ) -> TokenActivityIssue:
        if isinstance(error, ProviderBoundaryError):
            if error.failure.kind is ProviderFailureKind.UNREADABLE:
                kind = TokenActivityFailureKind.SOURCE_UNREADABLE
            elif error.failure.kind in {
                ProviderFailureKind.MALFORMED,
                ProviderFailureKind.INCOMPLETE,
            }:
                kind = TokenActivityFailureKind.SOURCE_MALFORMED
            else:
                kind = TokenActivityFailureKind.PROVIDER
            message = error.failure.message
        elif isinstance(error, AuthError):
            kind = TokenActivityFailureKind.AUTHENTICATION
            message = "Provider token activity authentication failed."
        elif isinstance(error, ForbiddenError):
            kind = TokenActivityFailureKind.FORBIDDEN
            message = "Provider token activity is forbidden."
        elif isinstance(error, RateLimitError):
            kind = TokenActivityFailureKind.RATE_LIMITED
            message = "Provider token activity is rate limited."
        elif isinstance(error, TransientError):
            kind = TokenActivityFailureKind.TRANSIENT
            message = "Provider token activity is temporarily unavailable."
        elif isinstance(error, InvalidPayloadError):
            kind = TokenActivityFailureKind.SOURCE_MALFORMED
            message = "Provider token activity response is malformed."
        elif isinstance(error, ActivitySnapshotError):
            kind = TokenActivityFailureKind.PERSISTENCE
            message = str(error)
        else:
            kind = TokenActivityFailureKind.PROVIDER
            message = "Provider token activity is unavailable."
        return TokenActivityIssue(kind=kind, message=message, label=label)
