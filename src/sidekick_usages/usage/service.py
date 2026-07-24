"""Provider-neutral account usage orchestration."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, assert_never

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialResolver,
)
from sidekick_usages.credentials.models import CredentialUpdateResult
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    ProviderIdentityError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import (
    CredentialRefresher,
    TokenMaintenanceService,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.providers.base import (
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
)
from sidekick_usages.usage.activity import (
    AccountTokenActivitySnapshots,
    AccountTokenActivitySource,
    LocalTokenActivitySource,
    TokenActivityCollector,
)
from sidekick_usages.usage.models import (
    AccountUsage,
    AuthenticationFailure,
    CredentialRecoveryKind,
    FetchFailure,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PersistenceFailure,
    ProviderPayloadFailure,
    RateLimitFailure,
    RefreshRejectedFailure,
    TransientFailure,
    UnknownProviderFailure,
    UsageCheckResult,
)

_CODEX_USAGE_REFRESH_MARGIN = timedelta(seconds=60)


type _CheckedAccount = _ActivityEligibleAccount | _ActivityIneligibleAccount


class CredentialCoordinator(CredentialRefresher, Protocol):
    """Credential mutations required by usage orchestration."""

    def persist_provider_update(
        self,
        account: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
    ) -> CredentialUpdateResult:
        """Persist provider-discovered credentials and plan atomically."""


@dataclass(frozen=True, slots=True)
class _ActivityEligibleAccount:
    label: AccountLabel
    provider_id: ProviderId
    outcome: AccountUsage | FetchFailure


@dataclass(frozen=True, slots=True)
class _ActivityIneligibleAccount:
    outcome: FetchFailure


class UsageCheckService:
    """Select accounts and return explicit usage-check outcomes."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[ProviderId, Provider],
        credentials: CredentialCoordinator,
        *,
        clock: Clock,
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
        activity_snapshots: AccountTokenActivitySnapshots | None = None,
        resolver: CredentialResolver,
    ) -> None:
        """Bind usage checking to its invocation-scoped dependencies.

        :param store: Loaded transactional account store.
        :param http: Shared provider HTTP facade.
        :param providers: Closed provider adapter registry.
        :param credentials: Canonical credential coordinator.
        :param clock: Aware application wall clock.
        :param local_activity_sources: Local-installation activity readers.
        :param account_activity_sources: Per-account activity readers.
        :param activity_snapshots: Durable last-successful account activity.
        :param resolver: Qualified schema-v3 credential resolver.
        """
        self._store = store
        self._http = http
        self._providers = providers
        self._credentials = credentials
        self._clock = clock
        self._resolver = resolver
        self._activity = TokenActivityCollector(
            http,
            ({} if local_activity_sources is None else local_activity_sources),
            (
                {}
                if account_activity_sources is None
                else account_activity_sources
            ),
            activity_snapshots,
            resolver,
        )
        self._maintenance = TokenMaintenanceService(
            store,
            credentials,
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
        saved_accounts = self._store.saved_accounts()
        reference_time = self._clock.now()
        usages: list[AccountUsage] = []
        failures: list[FetchFailure] = []
        eligible_accounts: list[Account] = []
        for account in accounts:
            checked = self._check_account(account, reference_time)
            if isinstance(checked, _ActivityEligibleAccount):
                eligible = self._store.get(
                    str(checked.label),
                    provider_id=checked.provider_id,
                )
                if eligible is not None:
                    eligible_accounts.append(eligible)
            outcome = checked.outcome
            if isinstance(outcome, AccountUsage):
                usages.append(outcome)
            else:
                failures.append(outcome)
        return UsageCheckResult(
            tuple(usages),
            tuple(failures),
            reference_time,
            self._activity.collect(
                accounts,
                tuple(eligible_accounts),
                reference_time,
                saved_accounts,
            ),
        )

    def _check_account(
        self,
        account: Account,
        reference_time: datetime,
    ) -> _CheckedAccount:
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return _ActivityIneligibleAccount(
                UnknownProviderFailure(
                    label=account.label,
                    provider_id=account.provider_id,
                    plan=account.plan,
                    message=f"Unknown provider '{account.provider_id}'.",
                )
            )
        expiry = classify_expiry(account.expiry, now=reference_time)
        if isinstance(expiry, InvalidExpiry):
            return _ActivityIneligibleAccount(
                InvalidExpiryFailure(
                    label=account.label,
                    provider_id=account.provider_id,
                    plan=account.plan,
                    message=(
                        "Access-token expiry metadata is invalid; refresh the "
                        "account."
                    ),
                )
            )
        if isinstance(expiry, ExpiredExpiry) or (
            isinstance(expiry, ValidExpiry)
            and provider.id is ProviderId.CODEX
            and expiry.at <= reference_time + _CODEX_USAGE_REFRESH_MARGIN
        ):
            refreshed = self._refresh(
                account,
                CredentialRefreshReason.CREDENTIAL_REQUIRED,
            )
            if isinstance(refreshed, FetchFailure):
                return _ActivityIneligibleAccount(refreshed)
            account = refreshed
        return self._fetch(account, provider, allow_auth_refresh=True)

    def _fetch(
        self,
        account: Account,
        provider: Provider,
        *,
        allow_auth_refresh: bool,
    ) -> _CheckedAccount:
        try:
            with self._open_account(account) as authenticated:
                return self._fetch_authenticated(
                    authenticated,
                    provider,
                )
        except AuthError as error:
            return self._handle_authentication(
                account,
                provider,
                error,
                allow_refresh=allow_auth_refresh,
            )

    def _fetch_authenticated(
        self,
        authenticated: AuthenticatedSavedAccount,
        provider: Provider,
    ) -> _CheckedAccount:
        """Fetch and persist provider state while one lease is active."""
        account = authenticated.lease.account
        before_credentials = account.credentials
        before_plan = account.plan
        try:
            report = provider.fetch_usage(authenticated, self._http)
        except AuthError:
            raise
        except ForbiddenError as error:
            return self._handle_forbidden(
                account,
                provider,
                error,
                before_credentials,
                before_plan,
            )
        except ProviderBoundaryError as error:
            return self._complete_failure(
                account,
                ProviderPayloadFailure(
                    label=account.label,
                    provider_id=account.provider_id,
                    plan=account.plan,
                    message=error.failure.message,
                    provider_failure=error.failure,
                ),
                before_credentials,
                before_plan,
            )
        except UsageError as error:
            if isinstance(error, ProviderIdentityError):
                return _ActivityIneligibleAccount(
                    self._failure_from_error(account, error)
                )
            return self._complete_failure(
                account,
                self._failure_from_error(account, error),
                before_credentials,
                before_plan,
            )

        outcome = self._complete_fetch(
            account,
            report,
            before_credentials,
            before_plan,
        )
        return self._checked_outcome(account, outcome)

    def _open_account(
        self,
        account: Account,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open one qualified credential lease."""
        saved = next(
            (
                candidate
                for candidate in self._store.saved_accounts()
                if candidate.provider_id is account.provider_id
                and candidate.label == account.label
            ),
            None,
        )
        if saved is None:
            raise SourceChangedError
        return self._resolver.open(saved)

    def _complete_failure(
        self,
        account: Account,
        failure: FetchFailure,
        before_credentials: Credentials,
        before_plan: str,
    ) -> _CheckedAccount:
        """Persist provider-discovered state before activity eligibility."""
        if (
            account.credentials != before_credentials
            or account.plan != before_plan
        ) and (
            persistence_failure := self._persist_provider_update(
                account,
                expected_credentials=before_credentials,
                expected_plan=before_plan,
            )
        ) is not None:
            return _ActivityIneligibleAccount(persistence_failure)
        return self._checked_outcome(account, failure)

    @staticmethod
    def _checked_outcome(
        account: Account,
        outcome: AccountUsage | FetchFailure,
    ) -> _CheckedAccount:
        if isinstance(
            outcome,
            AuthenticationFailure | PersistenceFailure,
        ):
            return _ActivityIneligibleAccount(outcome)
        return _ActivityEligibleAccount(
            account.label,
            account.provider_id,
            outcome,
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
        ) and (
            failure := self._persist_provider_update(
                account,
                expected_credentials=before_credentials,
                expected_plan=before_plan,
            )
        ) is not None:
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
    ) -> _CheckedAccount:
        if not allow_refresh:
            return _ActivityIneligibleAccount(
                self._failure_from_error(account, error)
            )
        refreshed = self._refresh(
            account,
            CredentialRefreshReason.ACCESS_REJECTED,
        )
        if isinstance(refreshed, FetchFailure):
            return _ActivityIneligibleAccount(refreshed)
        return self._fetch(
            refreshed,
            provider,
            allow_auth_refresh=False,
        )

    def _handle_forbidden(
        self,
        account: Account,
        provider: Provider,
        error: ForbiddenError,
        before_credentials: Credentials,
        before_plan: str,
    ) -> _CheckedAccount:
        return self._complete_failure(
            account,
            self._failure_from_error(account, error),
            before_credentials,
            before_plan,
        )

    def _refresh(
        self,
        account: Account,
        reason: CredentialRefreshReason,
    ) -> Account | FetchFailure:
        try:
            outcome = self._maintenance.refresh_account(
                account,
                force=True,
                reason=reason,
            )
        except PersistenceError as error:
            return self._persistence_failure(account, error)
        if outcome.refreshed:
            refreshed = self._store.get(
                str(account.label),
                provider_id=account.provider_id,
            )
            if refreshed is None:
                return self._persistence_failure(
                    account,
                    SourceChangedError(),
                )
            return refreshed
        if outcome.persistence_error is not None:
            return self._persistence_failure(
                account,
                outcome.persistence_error,
            )
        if outcome.operational_error is not None:
            return self._failure_from_error(
                account,
                outcome.operational_error,
            )
        return RefreshRejectedFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=outcome.message,
            credential_kind=_credential_recovery_kind(account.credentials),
            provider_failure=outcome.provider_failure,
        )

    def _persist_provider_update(
        self,
        account: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
    ) -> FetchFailure | None:
        """Delegate provider-discovered mutation to credential authority."""
        try:
            result = self._credentials.persist_provider_update(
                account,
                expected_credentials=expected_credentials,
                expected_plan=expected_plan,
            )
        except PersistenceError as error:
            return self._persistence_failure(account, error)
        if isinstance(result, ProviderFailure):
            return ProviderPayloadFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=result.message,
                provider_failure=result,
            )
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
                message=_authentication_cause(account.credentials),
                credential_kind=_credential_recovery_kind(account.credentials),
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


def _credential_recovery_kind(
    credentials: Credentials,
) -> CredentialRecoveryKind:
    """Classify credentials for presentation without exposing material."""
    match credentials:
        case ClaudeSetupTokenCredentials():
            return CredentialRecoveryKind.CLAUDE_SETUP_TOKEN
        case ClaudeLoginCredentials():
            return CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN
        case CodexCredentials():
            return CredentialRecoveryKind.CODEX_LOGIN
        case unexpected:
            assert_never(unexpected)


def _authentication_cause(credentials: Credentials) -> str:
    """Return one secret-safe cause owned by the credential boundary."""
    match credentials:
        case ClaudeSetupTokenCredentials():
            return "Claude rejected the saved setup token."
        case ClaudeLoginCredentials():
            return "Claude rejected the saved subscription login."
        case CodexCredentials():
            return "Codex rejected the saved login."
        case unexpected:
            assert_never(unexpected)
