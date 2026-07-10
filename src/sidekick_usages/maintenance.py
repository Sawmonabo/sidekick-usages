"""Saved-token refresh maintenance.

This module owns the scheduler-safe refresh behavior. It only uses
credentials already stored in sidekick-usages; it never imports the
current global Claude or Codex CLI login into an arbitrary account
label.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, assert_never

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ClassifiedExpiry,
    ExpiredExpiry,
    InvalidExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials import CredentialRefreshResult
from sidekick_usages.errors import RateLimitError, TransientError, UsageError
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind

CLAUDE_REFRESH_MARGIN_SECONDS = 30 * 60
CODEX_REFRESH_MARGIN_SECONDS = 10 * 60


class CredentialRefresher(Protocol):
    """Saved-credential refresh capability required by maintenance."""

    def refresh_saved(self, account: Account) -> CredentialRefreshResult:
        """Refresh and durably persist one saved account."""


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of one saved-token maintenance refresh."""

    label: AccountLabel
    provider_id: ProviderId
    status: RefreshStatus
    message: str
    exit_code: ExitCode = ExitCode.SUCCESS
    refreshed: bool = False
    action_required: bool = False
    provider_failure: ProviderFailure | None = None
    operational_error: UsageError | None = None
    persistence_error: PersistenceError | None = None


class TokenMaintenanceService:
    """Refresh saved provider tokens without adopting local logins."""

    def __init__(
        self,
        store: AccountStore,
        credentials: CredentialRefresher,
        *,
        clock: Clock,
    ) -> None:
        """:param store: Account store to update after each attempt.

        :param credentials: Canonical credential coordinator.
        :param clock: Aware UTC application wall clock.
        """
        self.store = store
        self.clock = clock
        self.credentials = credentials

    def refresh_all(
        self,
        *,
        provider_id: ProviderId | None = None,
        force: bool = False,
    ) -> list[RefreshOutcome]:
        """Refresh all matching accounts that are due.

        :param provider_id: Optional provider filter.
        :param force: Refresh every account with a saved refresh token.
        :return: Per-account outcomes in store order.
        """
        accounts = list(self.store)
        if provider_id is not None:
            accounts = [a for a in accounts if a.provider_id == provider_id]
        return [
            self.refresh_account(account, force=force) for account in accounts
        ]

    def refresh_account(
        self,
        account: Account,
        *,
        force: bool = False,
    ) -> RefreshOutcome:
        """Refresh one account if policy says it is due.

        :param account: Account to inspect and possibly mutate.
        :param force: Refresh even if the token is still fresh.
        :return: A scheduler-friendly outcome.
        """
        if force:
            should_refresh, expiry = (True, None)
        else:
            should_refresh, expiry = self._refresh_decision(
                account,
                force=False,
                reference_time=self.clock.now(),
            )
        if not should_refresh:
            return RefreshOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=RefreshStatus.SKIPPED,
                message=_skipped_message(expiry),
            )

        if not account.refresh_token:
            return self._record_failed(
                account,
                "No refresh token saved; log in manually.",
                exit_code=ExitCode.MANUAL_ACTION,
            )
        return self._refresh_due_account(account)

    def _refresh_due_account(
        self,
        account: Account,
    ) -> RefreshOutcome:
        """Refresh one policy-approved account through its configured path."""
        try:
            result = self.credentials.refresh_saved(account)
        except PersistenceError as error:
            return RefreshOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=RefreshStatus.FAILED,
                message="Refreshed credentials could not be persisted.",
                exit_code=ExitCode.SYSTEM_ERROR,
                persistence_error=error,
            )
        except RateLimitError as error:
            return self._record_failed(
                account,
                "Provider refresh was rate-limited; retry later.",
                exit_code=ExitCode.SYSTEM_ERROR,
                operational_error=RateLimitError(
                    "Provider refresh was rate-limited; retry later.",
                    retry_after=error.retry_after,
                ),
            )
        except TransientError:
            return self._record_failed(
                account,
                "Provider refresh is temporarily unavailable.",
                exit_code=ExitCode.SYSTEM_ERROR,
                operational_error=TransientError(
                    "Provider refresh is temporarily unavailable."
                ),
            )
        except UsageError:
            return self._record_failed(
                account,
                "Provider refresh could not be completed safely.",
                exit_code=ExitCode.SYSTEM_ERROR,
                operational_error=UsageError(
                    "Provider refresh could not be completed safely."
                ),
            )
        if isinstance(result, ProviderFailure):
            exit_code = (
                ExitCode.SYSTEM_ERROR
                if result.kind is ProviderFailureKind.UNSUPPORTED
                else ExitCode.MANUAL_ACTION
            )
            return self._record_failed(
                account,
                result.message,
                exit_code=exit_code,
                provider_failure=result,
            )
        return self._success_outcome(account)

    @staticmethod
    def _success_outcome(account: Account) -> RefreshOutcome:
        """Return one scheduler-facing successful refresh outcome."""
        return RefreshOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=RefreshStatus.OK,
            message="refreshed",
            refreshed=True,
        )

    def should_refresh(self, account: Account, *, force: bool = False) -> bool:
        """Return whether maintenance should refresh this account."""
        should_refresh, _ = self._refresh_decision(
            account,
            force=force,
            reference_time=self.clock.now(),
        )
        return should_refresh

    def _refresh_decision(
        self,
        account: Account,
        *,
        force: bool,
        reference_time: datetime,
    ) -> tuple[bool, ClassifiedExpiry | None]:
        """Return one refresh decision and its sampled expiry state."""
        if force:
            return (True, None)
        expiry = self.expiry(account, reference_time)
        if not account.refresh_token:
            return (isinstance(expiry, InvalidExpiry), expiry)
        if isinstance(expiry, ExpiredExpiry | InvalidExpiry):
            return (True, expiry)
        if isinstance(expiry, ValidExpiry):
            margin = timedelta(
                seconds=refresh_margin_seconds(account.provider_id)
            )
            return (expiry.at <= reference_time + margin, expiry)
        return (False, expiry)

    def expiry(
        self,
        account: Account,
        reference_time: datetime,
    ) -> ClassifiedExpiry:
        """Classify account expiry against one explicit reference time."""
        return classify_expiry(account.expiry, now=reference_time)

    def _record_failed(
        self,
        account: Account,
        message: str,
        *,
        exit_code: ExitCode,
        provider_failure: ProviderFailure | None = None,
        operational_error: UsageError | None = None,
    ) -> RefreshOutcome:
        """Persist a failed refresh diagnostic and return its outcome."""
        record_refresh_failure(account, message, self.clock.now())
        self.store.persist(account)
        return RefreshOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=RefreshStatus.FAILED,
            message=message,
            exit_code=exit_code,
            action_required=exit_code == ExitCode.MANUAL_ACTION,
            provider_failure=provider_failure,
            operational_error=operational_error,
        )


def refresh_margin_seconds(provider_id: ProviderId) -> int:
    """Return the provider-specific proactive refresh margin."""
    if provider_id == ProviderId.CLAUDE:
        return CLAUDE_REFRESH_MARGIN_SECONDS
    if provider_id == ProviderId.CODEX:
        return CODEX_REFRESH_MARGIN_SECONDS
    assert_never(provider_id)


def record_refresh_failure(
    account: Account,
    message: str,
    reference_time: datetime,
) -> None:
    """Mark an account's latest refresh as failed."""
    account.last_refresh_at = reference_time
    account.last_refresh_status = RefreshStatus.FAILED
    account.last_refresh_error = message


def refresh_exit_code(outcomes: list[RefreshOutcome]) -> ExitCode:
    """Collapse per-account outcomes into the documented CLI exit code."""
    if any(outcome.exit_code == ExitCode.SYSTEM_ERROR for outcome in outcomes):
        return ExitCode.SYSTEM_ERROR
    if any(
        outcome.exit_code == ExitCode.MANUAL_ACTION for outcome in outcomes
    ):
        return ExitCode.MANUAL_ACTION
    return ExitCode.SUCCESS


def _skipped_message(expiry: ClassifiedExpiry | None) -> str:
    """Return stable human detail for a skipped refresh."""
    if expiry is None:
        return "unknown"
    if isinstance(expiry, ValidExpiry):
        return "fresh"
    if isinstance(expiry, UnknownExpiry):
        return "unknown"
    return expiry.state.value
