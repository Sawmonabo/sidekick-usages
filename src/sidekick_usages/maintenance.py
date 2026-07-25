"""Saved-token refresh maintenance.

This module owns the scheduler-safe refresh behavior. It only uses
credentials already stored in sidekick-usages; it never imports the
current global Claude or Codex CLI login into an arbitrary account
label.
"""

from dataclasses import dataclass, replace
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
    refresh_due,
)
from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.claude.lifetime import (
    ClaudeLoginRenewalState,
    classify_claude_login_renewal,
)
from sidekick_usages.credentials.models import CredentialRefreshResult
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.errors import RateLimitError, TransientError, UsageError
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from sidekick_usages.providers.codex.token import CODEX_REFRESH_MARGIN

CLAUDE_REFRESH_MARGIN = timedelta(minutes=30)


class CredentialRefresher(Protocol):
    """Saved-credential refresh capability required by maintenance."""

    def refresh(
        self,
        *,
        provider_id: ProviderId,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        """Refresh and durably persist one exact saved account."""


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
    login_renewal_state: ClaudeLoginRenewalState = (
        ClaudeLoginRenewalState.NOT_APPLICABLE
    )

    @property
    def login_renewal_message(self) -> str | None:
        """Return the display-safe advisory for the derived login state."""
        return _login_renewal_message(self.login_renewal_state)


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
        reason: CredentialRefreshReason | None = None,
    ) -> RefreshOutcome:
        """Refresh one account if policy says it is due.

        :param account: Account to inspect and possibly mutate.
        :param force: Refresh even if the token is still fresh.
        :return: A scheduler-friendly outcome.
        """
        selected_reason = reason or (
            CredentialRefreshReason.OPERATOR_FORCED
            if force
            else CredentialRefreshReason.SCHEDULED_DUE
        )
        reference_time = self.clock.now()
        login_renewal_state = classify_claude_login_renewal(
            account.credentials,
            reference_time=reference_time,
        )
        if login_renewal_state in (
            ClaudeLoginRenewalState.EXPIRED,
            ClaudeLoginRenewalState.INVALID,
        ):
            return _login_renewal_outcome(
                account,
                login_renewal_state,
                refreshed=False,
            )
        if force or reason is not None:
            should_refresh, expiry = (True, None)
        else:
            should_refresh, expiry = self._refresh_decision(
                account,
                force=False,
                reference_time=reference_time,
            )
        if not should_refresh:
            return _skipped_outcome(
                account,
                expiry=expiry,
                login_renewal_state=login_renewal_state,
            )

        if not account.refresh_token and not isinstance(
            account.credentials,
            ClaudeSetupTokenCredentials,
        ):
            return replace(
                self._record_failed(
                    account,
                    "No refresh token saved; log in manually.",
                    exit_code=ExitCode.MANUAL_ACTION,
                ),
                login_renewal_state=login_renewal_state,
            )
        outcome = replace(
            self._refresh_due_account(account, selected_reason),
            login_renewal_state=login_renewal_state,
        )
        if outcome.status is not RefreshStatus.OK:
            return outcome
        saved = self.store.get(str(account.label))
        refreshed_state = classify_claude_login_renewal(
            (saved or account).credentials,
            reference_time=reference_time,
        )
        if refreshed_state is ClaudeLoginRenewalState.RENEWAL_DUE:
            return _login_renewal_outcome(
                account,
                refreshed_state,
                refreshed=True,
            )
        return RefreshOutcome(
            label=outcome.label,
            provider_id=outcome.provider_id,
            status=outcome.status,
            message=outcome.message,
            exit_code=outcome.exit_code,
            refreshed=outcome.refreshed,
            action_required=outcome.action_required,
            provider_failure=outcome.provider_failure,
            operational_error=outcome.operational_error,
            persistence_error=outcome.persistence_error,
            login_renewal_state=refreshed_state,
        )

    def _refresh_due_account(
        self,
        account: Account,
        reason: CredentialRefreshReason,
    ) -> RefreshOutcome:
        """Refresh one policy-approved account through its configured path."""
        try:
            result = self.credentials.refresh(
                provider_id=account.provider_id,
                label=account.label,
                reason=reason,
            )
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
        return (
            refresh_due(
                expiry,
                now=reference_time,
                margin=refresh_margin(account.provider_id),
            ),
            expiry,
        )

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


def refresh_margin(provider_id: ProviderId) -> timedelta:
    """Return the provider-specific proactive refresh margin."""
    if provider_id == ProviderId.CLAUDE:
        return CLAUDE_REFRESH_MARGIN
    if provider_id == ProviderId.CODEX:
        return CODEX_REFRESH_MARGIN
    assert_never(provider_id)


def _login_renewal_outcome(
    account: Account,
    state: ClaudeLoginRenewalState,
    *,
    refreshed: bool,
) -> RefreshOutcome:
    """Return one derived manual action without persisting a failure."""
    message = _login_renewal_message(state)
    if message is None:
        raise AssertionError(f"Unexpected renewal action state: {state!r}")
    if refreshed:
        message = "Access token refreshed; " + message
    return RefreshOutcome(
        label=account.label,
        provider_id=account.provider_id,
        status=RefreshStatus.OK if refreshed else RefreshStatus.SKIPPED,
        message=message,
        exit_code=ExitCode.MANUAL_ACTION,
        refreshed=refreshed,
        action_required=True,
        login_renewal_state=state,
    )


def _login_renewal_message(
    state: ClaudeLoginRenewalState,
) -> str | None:
    """Return one canonical display message for a derived renewal state."""
    if state is ClaudeLoginRenewalState.RENEWAL_DUE:
        return "Claude login expires within five days."
    if state is ClaudeLoginRenewalState.EXPIRED:
        return "Claude login has expired."
    if state is ClaudeLoginRenewalState.INVALID:
        return "Claude login expiry is invalid."
    return None


def _skipped_outcome(
    account: Account,
    *,
    expiry: ClassifiedExpiry | None,
    login_renewal_state: ClaudeLoginRenewalState,
) -> RefreshOutcome:
    """Return one ordinary skip or derived login-renewal action."""
    if login_renewal_state is ClaudeLoginRenewalState.RENEWAL_DUE:
        return _login_renewal_outcome(
            account,
            login_renewal_state,
            refreshed=False,
        )
    return RefreshOutcome(
        label=account.label,
        provider_id=account.provider_id,
        status=RefreshStatus.SKIPPED,
        message=_skipped_message(expiry),
        login_renewal_state=login_renewal_state,
    )


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
