"""One-account provider lookup under one operation authority."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import Account, Credentials, UsageReport
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    AuthorizedCredentialResolver,
    CredentialResolver,
    HeldAuthorizedCredentialResolver,
)
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    ProviderIdentityError,
    UsageError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.providers.base import (
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.usage.activity import TokenActivityCollector
from sidekick_usages.usage.failures import failure_from_error
from sidekick_usages.usage.lookup.models import (
    AccountLookupReading,
    AccountMutationExchange,
    AccountRefreshResult,
    ActivityObservation,
    CredentialRefreshIntent,
    CurrentUsageReading,
    ProviderStateIntent,
    ProviderStateResult,
)
from sidekick_usages.usage.lookup.ports import AccountOperationLocks
from sidekick_usages.usage.models import (
    AuthenticationFailure,
    FetchFailure,
    InvalidExpiryFailure,
    PersistenceFailure,
    ProviderPayloadFailure,
    UnknownProviderFailure,
)

_CODEX_USAGE_REFRESH_MARGIN = timedelta(seconds=60)


class AccountCredentialAccess:
    """Bind account locks to authority-aware credential resolution."""

    def __init__(
        self,
        resolver: AuthorizedCredentialResolver,
        operation_locks: AccountOperationLocks,
    ) -> None:
        self._resolver = resolver
        self._operation_locks = operation_locks

    def hold(
        self,
        account_id: SidekickAccountId,
    ) -> AbstractContextManager[CredentialResolver]:
        """Hold one account authority and bind credential resolution to it."""
        return self._hold(account_id)

    @contextmanager
    def _hold(
        self,
        account_id: SidekickAccountId,
    ) -> Iterator[CredentialResolver]:
        with self._operation_locks.hold(account_id) as authority:
            yield HeldAuthorizedCredentialResolver(
                self._resolver,
                authority,
            )


class AccountLookupService:
    """Fetch one saved account without mutating shared durable state."""

    def __init__(
        self,
        http: HttpClient,
        providers: dict[ProviderId, Provider],
        activity: TokenActivityCollector,
        credentials: AccountCredentialAccess,
    ) -> None:
        """Bind lookup to shared read boundaries and account locks."""
        self._http = http
        self._providers = providers
        self._activity = activity
        self._credentials = credentials

    def lookup(
        self,
        account: SavedAccount,
        ordinal: int,
        reference_time: datetime,
        mutate: AccountMutationExchange,
    ) -> AccountLookupReading:
        """Return one immutable reading under its complete operation lock."""
        with self._credentials.hold(account.account_id) as resolver:
            return self._lookup_locked(
                account,
                ordinal,
                reference_time,
                mutate,
                resolver,
            )

    def _lookup_locked(
        self,
        account: SavedAccount,
        ordinal: int,
        reference_time: datetime,
        mutate: AccountMutationExchange,
        resolver: CredentialResolver,
    ) -> AccountLookupReading:
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return self._reading(
                account,
                ordinal,
                UnknownProviderFailure(
                    label=account.label,
                    provider_id=account.provider_id,
                    plan=account.plan,
                    message=f"Unknown provider '{account.provider_id}'.",
                ),
                activity_eligible=False,
            )
        expiry = classify_expiry(account.access_expiry, now=reference_time)
        if isinstance(expiry, InvalidExpiry):
            return self._reading(
                account,
                ordinal,
                InvalidExpiryFailure(
                    label=account.label,
                    provider_id=account.provider_id,
                    plan=account.plan,
                    message=(
                        "Access-token expiry metadata is invalid; refresh the "
                        "account."
                    ),
                ),
                activity_eligible=False,
            )
        if not account.has_managed_authority and (
            isinstance(expiry, ExpiredExpiry)
            or (
                isinstance(expiry, ValidExpiry)
                and provider.id is ProviderId.CODEX
                and expiry.at <= reference_time + _CODEX_USAGE_REFRESH_MARGIN
            )
        ):
            refreshed = self._refresh(
                account,
                CredentialRefreshReason.CREDENTIAL_REQUIRED,
                mutate,
            )
            if isinstance(refreshed, FetchFailure):
                return self._reading(
                    account,
                    ordinal,
                    refreshed,
                    activity_eligible=False,
                )
            account = refreshed
        return self._fetch(
            account,
            ordinal,
            provider,
            reference_time,
            mutate,
            resolver,
            allow_auth_refresh=True,
        )

    def _fetch(
        self,
        account: SavedAccount,
        ordinal: int,
        provider: Provider,
        reference_time: datetime,
        mutate: AccountMutationExchange,
        resolver: CredentialResolver,
        *,
        allow_auth_refresh: bool,
    ) -> AccountLookupReading:
        try:
            with self._open_account(account, resolver) as authenticated:
                outcome, activity_eligible = self._fetch_authenticated(
                    authenticated,
                    provider,
                    mutate,
                )
                activity = (
                    self._activity.read_account(authenticated)
                    if activity_eligible
                    else None
                )
                return self._reading(
                    account,
                    ordinal,
                    outcome,
                    activity_eligible=activity_eligible,
                    activity=activity,
                )
        except AuthError as error:
            return self._handle_authentication(
                account,
                ordinal,
                provider,
                error,
                reference_time,
                mutate,
                resolver,
                allow_refresh=allow_auth_refresh,
            )
        except UsageError as error:
            return self._reading(
                account,
                ordinal,
                failure_from_error(account, error),
                activity_eligible=False,
            )

    def _fetch_authenticated(
        self,
        authenticated: AuthenticatedSavedAccount,
        provider: Provider,
        mutate: AccountMutationExchange,
    ) -> tuple[CurrentUsageReading | FetchFailure, bool]:
        """Fetch usage and activity through one healthy-path lease."""
        saved = authenticated.account
        runtime = authenticated.lease.account
        before_credentials = runtime.credentials
        before_plan = runtime.plan
        identity_valid = True
        try:
            report = provider.fetch_usage(authenticated, self._http)
        except AuthError:
            raise
        except ForbiddenError as error:
            outcome = self._complete_failure(
                saved,
                runtime,
                failure_from_error(saved, error),
                before_credentials,
                before_plan,
                mutate,
            )
        except ProviderBoundaryError as error:
            outcome = self._complete_failure(
                saved,
                runtime,
                ProviderPayloadFailure(
                    label=saved.label,
                    provider_id=saved.provider_id,
                    plan=saved.plan,
                    message=error.failure.message,
                    provider_failure=error.failure,
                ),
                before_credentials,
                before_plan,
                mutate,
            )
        except UsageError as error:
            if isinstance(error, ProviderIdentityError):
                outcome = failure_from_error(saved, error)
                identity_valid = False
            else:
                outcome = self._complete_failure(
                    saved,
                    runtime,
                    failure_from_error(saved, error),
                    before_credentials,
                    before_plan,
                    mutate,
                )
        else:
            outcome = self._complete_fetch(
                saved,
                runtime,
                report,
                before_credentials,
                before_plan,
                mutate,
            )
        return outcome, identity_valid and self._activity_eligible(outcome)

    def _complete_failure(
        self,
        account: SavedAccount,
        runtime: Account,
        failure: FetchFailure,
        before_credentials: Credentials,
        before_plan: str,
        mutate: AccountMutationExchange,
    ) -> FetchFailure:
        """Persist safe provider state before activity eligibility."""
        persistence_failure_result = self._persist_provider_state(
            account,
            runtime,
            expected_credentials=before_credentials,
            expected_plan=before_plan,
            mutate=mutate,
        )
        return persistence_failure_result or failure

    def _complete_fetch(
        self,
        account: SavedAccount,
        runtime: Account,
        report: UsageReport,
        before_credentials: Credentials,
        before_plan: str,
        mutate: AccountMutationExchange,
    ) -> CurrentUsageReading | FetchFailure:
        """Apply safe provider state before exposing current usage."""
        if report.plan and report.plan not in {"unknown", runtime.plan}:
            runtime.plan = report.plan
        failure = self._persist_provider_state(
            account,
            runtime,
            expected_credentials=before_credentials,
            expected_plan=before_plan,
            mutate=mutate,
        )
        if failure is not None:
            return failure
        return CurrentUsageReading(plan=runtime.plan, report=report)

    def _persist_provider_state(
        self,
        account: SavedAccount,
        runtime: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
        mutate: AccountMutationExchange,
    ) -> FetchFailure | None:
        """Send only secret-free provider state to the owner thread."""
        if runtime.credentials != expected_credentials:
            message = (
                "Provider usage changed credentials outside the refresh "
                "authority."
            )
            return ProviderPayloadFailure(
                label=account.label,
                provider_id=account.provider_id,
                plan=account.plan,
                message=message,
                provider_failure=ProviderFailure(
                    provider_id=account.provider_id,
                    kind=ProviderFailureKind.IDENTITY_MISMATCH,
                    message=message,
                ),
            )
        if runtime.plan == expected_plan:
            return None
        result = mutate(
            ProviderStateIntent(
                account=account,
                plan=runtime.plan,
            )
        )
        if not isinstance(result, ProviderStateResult):
            raise TypeError("Provider-state mutation returned wrong result.")
        return result.failure

    def _handle_authentication(
        self,
        account: SavedAccount,
        ordinal: int,
        provider: Provider,
        error: AuthError,
        reference_time: datetime,
        mutate: AccountMutationExchange,
        resolver: CredentialResolver,
        *,
        allow_refresh: bool,
    ) -> AccountLookupReading:
        if not allow_refresh or account.has_managed_authority:
            return self._reading(
                account,
                ordinal,
                failure_from_error(account, error),
                activity_eligible=False,
            )
        refreshed = self._refresh(
            account,
            CredentialRefreshReason.ACCESS_REJECTED,
            mutate,
        )
        if isinstance(refreshed, FetchFailure):
            return self._reading(
                account,
                ordinal,
                refreshed,
                activity_eligible=False,
            )
        return self._fetch(
            refreshed,
            ordinal,
            provider,
            reference_time,
            mutate,
            resolver,
            allow_auth_refresh=False,
        )

    @staticmethod
    def _refresh(
        account: SavedAccount,
        reason: CredentialRefreshReason,
        mutate: AccountMutationExchange,
    ) -> SavedAccount | FetchFailure:
        result = mutate(
            CredentialRefreshIntent(
                account=account,
                reason=reason,
            )
        )
        if not isinstance(result, AccountRefreshResult):
            raise TypeError("Credential refresh returned wrong result.")
        if result.account is not None:
            return result.account
        if result.failure is None:
            raise AssertionError("Credential refresh result disappeared.")
        return result.failure

    @staticmethod
    def _activity_eligible(
        outcome: CurrentUsageReading | FetchFailure,
    ) -> bool:
        """Return whether one active lease may fetch account activity."""
        return not isinstance(
            outcome,
            AuthenticationFailure | PersistenceFailure,
        )

    @staticmethod
    def _reading(
        account: SavedAccount,
        ordinal: int,
        outcome: CurrentUsageReading | FetchFailure,
        *,
        activity_eligible: bool,
        activity: ActivityObservation | None = None,
    ) -> AccountLookupReading:
        """Build one closed immutable thread result."""
        return AccountLookupReading(
            ordinal=ordinal,
            account_id=account.account_id,
            label=account.label,
            provider_id=account.provider_id,
            usage=(
                outcome if isinstance(outcome, CurrentUsageReading) else None
            ),
            failure=outcome if isinstance(outcome, FetchFailure) else None,
            activity=activity,
            activity_eligible=activity_eligible,
        )

    def _open_account(
        self,
        account: SavedAccount,
        resolver: CredentialResolver,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open one qualified credential lease."""
        return resolver.open(account)
