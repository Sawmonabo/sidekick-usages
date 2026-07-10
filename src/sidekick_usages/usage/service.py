"""Provider-neutral account usage orchestration."""

from dataclasses import replace
from datetime import datetime, timedelta

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    Credentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.providers.base import Provider
from sidekick_usages.usage.models import (
    AccountUsage,
    AuthenticationFailure,
    FetchFailure,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PersistenceFailure,
    RateLimitFailure,
    RefreshRejectedFailure,
    TransientFailure,
    UnknownProviderFailure,
    UsageCheckResult,
)

_CODEX_USAGE_REFRESH_MARGIN = timedelta(seconds=60)
_CLAUDE_USAGE_REQUIRED_SCOPE = "user:profile"


class UsageCheckService:
    """Select accounts and return explicit usage-check outcomes."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[ProviderId, Provider],
        *,
        clock: Clock,
    ) -> None:
        """Bind usage checking to its invocation-scoped dependencies.

        :param store: Loaded transactional account store.
        :param http: Shared provider HTTP facade.
        :param providers: Closed provider adapter registry.
        :param clock: Aware application wall clock.
        """
        self._store = store
        self._http = http
        self._providers = providers
        self._clock = clock
        self._maintenance = TokenMaintenanceService(
            store,
            http,
            providers,
            clock=clock,
        )

    def check(
        self,
        provider_id: ProviderId | None = None,
    ) -> UsageCheckResult:
        """Check every selected account without rendering or exiting.

        :param provider_id: Optional provider filter.
        :returns: Immutable successes and terminal failures in store order.
        """
        accounts = (
            tuple(self._store)
            if provider_id is None
            else tuple(self._store.filter_by_provider(provider_id))
        )
        reference_time = self._clock.now()
        usages: list[AccountUsage] = []
        failures: list[FetchFailure] = []
        for account in accounts:
            outcome = self._check_account(account, reference_time)
            if isinstance(outcome, AccountUsage):
                usages.append(outcome)
            else:
                failures.append(outcome)
        return UsageCheckResult(tuple(usages), tuple(failures))

    def _check_account(
        self,
        account: Account,
        reference_time: datetime,
    ) -> AccountUsage | FetchFailure:
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return UnknownProviderFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=f"Unknown provider '{account.provider_id}'.",
            )
        expiry = classify_expiry(account.expiry, now=reference_time)
        if isinstance(expiry, InvalidExpiry):
            return InvalidExpiryFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=(
                    "Access-token expiry metadata is invalid; refresh the "
                    "account."
                ),
            )
        if (
            isinstance(expiry, ExpiredExpiry)
            or (
                isinstance(expiry, ValidExpiry)
                and provider.id is ProviderId.CODEX
                and expiry.at <= reference_time + _CODEX_USAGE_REFRESH_MARGIN
            )
        ) and (failure := self._refresh(account)) is not None:
            return failure
        return self._fetch(account, provider, allow_auth_refresh=True)

    def _fetch(
        self,
        account: Account,
        provider: Provider,
        *,
        allow_auth_refresh: bool,
    ) -> AccountUsage | FetchFailure:
        before_credentials = account.credentials
        before_plan = account.plan
        try:
            report = provider.fetch_usage(account, self._http)
        except AuthError as error:
            return self._handle_authentication(
                account,
                provider,
                error,
                allow_refresh=allow_auth_refresh,
            )
        except ForbiddenError as error:
            return self._handle_forbidden(account, provider, error)
        except UsageError as error:
            return self._failure_from_error(account, error)

        return self._complete_fetch(
            account,
            report,
            before_credentials,
            before_plan,
        )

    def _complete_fetch(
        self,
        account: Account,
        report: UsageReport,
        before_credentials: Credentials,
        before_plan: str,
    ) -> AccountUsage | FetchFailure:
        """Apply validated provider changes before exposing usage."""

        if report.plan and report.plan not in {"unknown", account.plan}:
            account.plan = report.plan
        if (
            account.credentials != before_credentials
            or account.plan != before_plan
        ) and (failure := self._persist(account)) is not None:
            return failure
        return AccountUsage(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            report=report,
        )

    def _handle_authentication(
        self,
        account: Account,
        provider: Provider,
        error: AuthError,
        *,
        allow_refresh: bool,
    ) -> AccountUsage | FetchFailure:
        if not allow_refresh:
            return self._failure_from_error(account, error)
        if (failure := self._refresh(account)) is not None:
            return failure
        return self._fetch(account, provider, allow_auth_refresh=False)

    def _handle_forbidden(
        self,
        account: Account,
        provider: Provider,
        error: ForbiddenError,
    ) -> AccountUsage | FetchFailure:
        credentials = account.credentials
        if (
            provider.id is ProviderId.CLAUDE
            and isinstance(credentials, ClaudeCredentials)
            and credentials.scopes is None
            and error.required_scope == _CLAUDE_USAGE_REQUIRED_SCOPE
        ):
            account.credentials = replace(credentials, scopes=())
            if (failure := self._persist(account)) is not None:
                return failure
            return self._fetch(
                account,
                provider,
                allow_auth_refresh=False,
            )
        return self._failure_from_error(account, error)

    def _refresh(self, account: Account) -> FetchFailure | None:
        try:
            outcome = self._maintenance.refresh_account(account, force=True)
        except PersistenceError as error:
            return self._persistence_failure(account, error)
        if outcome.refreshed:
            return None
        return RefreshRejectedFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=outcome.message,
        )

    def _persist(self, account: Account) -> PersistenceFailure | None:
        try:
            self._store.persist(account)
        except PersistenceError as error:
            return self._persistence_failure(account, error)
        return None

    @staticmethod
    def _persistence_failure(
        account: Account,
        error: PersistenceError,
    ) -> PersistenceFailure:
        return PersistenceFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=str(error),
            persistence_code=error.code,
        )

    @staticmethod
    def _failure_from_error(
        account: Account,
        error: UsageError,
    ) -> FetchFailure:
        if isinstance(error, PersistenceError):
            return PersistenceFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=str(error),
                persistence_code=error.code,
            )
        if isinstance(error, AuthError):
            return AuthenticationFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=str(error),
            )
        if isinstance(error, ForbiddenError):
            return ForbiddenFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=error.api_message or str(error),
                required_scope=error.required_scope,
            )
        if isinstance(error, RateLimitError):
            return RateLimitFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=str(error),
                retry_after_seconds=error.retry_after,
            )
        if isinstance(error, TransientError):
            return TransientFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=str(error),
            )
        return FetchFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=str(error),
        )


__all__ = ["UsageCheckService"]
