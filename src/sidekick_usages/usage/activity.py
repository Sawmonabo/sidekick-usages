"""Application policy for scoped provider token activity."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

from sidekick_usages.core.models import (
    Account,
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


class TokenActivityCollector:
    """Collect and aggregate provider activity for one account selection."""

    def __init__(
        self,
        http: HttpClient,
        local_sources: Mapping[ProviderId, LocalTokenActivitySource],
        account_sources: Mapping[ProviderId, AccountTokenActivitySource],
    ) -> None:
        """Bind collection to validated provider source mappings."""
        self._http = http
        self._local_sources = dict(local_sources)
        self._account_sources = dict(account_sources)
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
    ) -> ProviderTokenActivity:
        total = 0
        since_dates: list[date] = []
        has_unknown_since = False
        covered = 0
        issues: list[TokenActivityIssue] = []
        for account in eligible:
            try:
                reading = source.read(account, self._http)
            except UsageError as error:
                issues.append(self._issue(error, account.label))
                continue
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
                continue
            if isinstance(reading, TokenActivityUnavailable):
                continue
            if reading.total_tokens > _MAX_TOKEN_COUNT - total:
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
            total += reading.total_tokens
            covered += 1
            if reading.since is None:
                has_unknown_since = True
            else:
                since_dates.append(reading.since)

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
            )
        return PartialTokenActivity(
            provider_id=provider_id,
            summary=summary,
            covered_accounts=covered,
            selected_accounts=len(selected),
            issues=tuple(issues),
        )

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
        else:
            kind = TokenActivityFailureKind.PROVIDER
            message = "Provider token activity is unavailable."
        return TokenActivityIssue(kind=kind, message=message, label=label)


__all__ = [
    "AccountTokenActivitySource",
    "LocalTokenActivitySource",
    "TokenActivityCollector",
]
