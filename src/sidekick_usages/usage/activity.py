"""Application policy for scoped provider token activity."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

from sidekick_usages.core.models import (
    Account,
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
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    InvalidPayloadError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.activity_snapshots import (
    ActivitySnapshotError,
)
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
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
        account: Account,
        http: HttpClient,
    ) -> TokenActivityReading:
        """Return one account-scoped activity reading."""


class AccountTokenActivitySnapshots(Protocol):
    """Persist authoritative activity by stable account identity."""

    def load(self, account: Account) -> AccountTokenActivitySnapshot | None:
        """Load the account's last successful activity snapshot."""

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        """Durably merge one successful account activity snapshot."""


class TokenActivityCollector:
    """Collect and aggregate provider activity for one account selection."""

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

    def collect(
        self,
        accounts: tuple[Account, ...],
        eligible_accounts: tuple[Account, ...],
        reference_time: datetime,
    ) -> tuple[ProviderTokenActivity, ...]:
        """Collect activity once from the selected provider population."""
        provider_order = tuple(dict.fromkeys(a.provider_id for a in accounts))
        outcomes: list[ProviderTokenActivity] = []
        for provider_id in provider_order:
            selected = tuple(
                account
                for account in accounts
                if account.provider_id is provider_id
            )
            eligible = tuple(
                account
                for account in eligible_accounts
                if account.provider_id is provider_id
            )
            local_source = self._local_sources.get(provider_id)
            account_source = self._account_sources.get(provider_id)
            if local_source is not None:
                outcomes.append(
                    self._collect_local(
                        provider_id,
                        local_source,
                        reference_time,
                    )
                )
            elif account_source is not None:
                outcomes.append(
                    self._collect_accounts(
                        provider_id,
                        account_source,
                        selected,
                        eligible,
                        reference_time,
                    )
                )
        return tuple(outcomes)

    @classmethod
    def _collect_local(
        cls,
        provider_id: ProviderId,
        source: LocalTokenActivitySource,
        reference_time: datetime,
    ) -> ProviderTokenActivity:
        try:
            reading = source.read(reference_time)
        except UsageError as error:
            return FailedTokenActivity(
                provider_id=provider_id,
                scope=TokenActivityScope.LOCAL_INSTALLATION,
                issues=(cls._issue(error),),
            )
        if reading.scope is not TokenActivityScope.LOCAL_INSTALLATION:
            return cls._invalid_scope(
                provider_id,
                TokenActivityScope.LOCAL_INSTALLATION,
            )
        if isinstance(reading, TokenActivityUnavailable):
            return UnavailableTokenActivity(
                provider_id=provider_id,
                scope=reading.scope,
            )
        return CompleteTokenActivity(
            provider_id=provider_id,
            summary=reading,
        )

    def _collect_accounts(
        self,
        provider_id: ProviderId,
        source: AccountTokenActivitySource,
        selected: tuple[Account, ...],
        eligible: tuple[Account, ...],
        reference_time: datetime,
    ) -> ProviderTokenActivity:
        total = 0
        since_dates: list[date] = []
        has_unknown_since = False
        covered = 0
        issues: list[TokenActivityIssue] = []
        eligible_by_label = {account.label: account for account in eligible}
        for selected_account in selected:
            account = eligible_by_label.get(selected_account.label)
            summary, account_issues = self._account_summary(
                source,
                selected_account if account is None else account,
                reference_time,
                fetch=account is not None,
            )
            issues.extend(account_issues)
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
                        label=selected_account.label,
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
            selected_accounts=len(selected),
            issues=tuple(issues),
        )

    def _account_summary(
        self,
        source: AccountTokenActivitySource,
        account: Account,
        reference_time: datetime,
        *,
        fetch: bool,
    ) -> tuple[TokenActivitySummary | None, tuple[TokenActivityIssue, ...]]:
        issues: list[TokenActivityIssue] = []
        if fetch:
            try:
                reading = source.read(account, self._http)
            except UsageError as error:
                issues.append(self._issue(error, account.label))
            else:
                if reading.scope is not TokenActivityScope.ACCOUNT:
                    issues.append(
                        TokenActivityIssue(
                            kind=TokenActivityFailureKind.PROVIDER,
                            message=(
                                "Provider token activity returned an invalid "
                                "scope."
                            ),
                            label=account.label,
                        )
                    )
                elif isinstance(reading, TokenActivitySummary):
                    summary, snapshot_issue = self._save_snapshot(
                        account,
                        reading,
                        reference_time,
                    )
                    if snapshot_issue is not None:
                        issues.append(snapshot_issue)
                    return summary, tuple(issues)
        snapshot, snapshot_issue = self._load_snapshot(account)
        if snapshot_issue is not None:
            issues.append(snapshot_issue)
        return (
            None if snapshot is None else snapshot.summary,
            tuple(issues),
        )

    def _load_snapshot(
        self,
        account: Account,
    ) -> tuple[
        AccountTokenActivitySnapshot | None,
        TokenActivityIssue | None,
    ]:
        if self._snapshots is None:
            return None, None
        try:
            return self._snapshots.load(account), None
        except ActivitySnapshotError as error:
            return None, self._issue(error, account.label)

    def _save_snapshot(
        self,
        account: Account,
        summary: TokenActivitySummary,
        reference_time: datetime,
    ) -> tuple[TokenActivitySummary, TokenActivityIssue | None]:
        account_id = account.provider_account_id
        if self._snapshots is None or account_id is None:
            return summary, None
        snapshot = AccountTokenActivitySnapshot(
            provider_id=account.provider_id,
            provider_account_id=account_id,
            summary=summary,
            fetched_at=reference_time,
        )
        try:
            durable = self._snapshots.save(snapshot)
        except ActivitySnapshotError as error:
            return summary, self._issue(error, account.label)
        return durable.summary, None

    @staticmethod
    def _invalid_scope(
        provider_id: ProviderId,
        scope: TokenActivityScope,
    ) -> FailedTokenActivity:
        return FailedTokenActivity(
            provider_id=provider_id,
            scope=scope,
            issues=(
                TokenActivityIssue(
                    kind=TokenActivityFailureKind.PROVIDER,
                    message=(
                        "Provider token activity returned an invalid scope."
                    ),
                ),
            ),
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


__all__ = [
    "AccountTokenActivitySnapshots",
    "AccountTokenActivitySource",
    "LocalTokenActivitySource",
    "TokenActivityCollector",
]
