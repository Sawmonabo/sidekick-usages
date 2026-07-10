"""Saved-token refresh maintenance.

This module owns the scheduler-safe refresh behavior. It only uses
credentials already stored in sidekick-usages; it never imports the
current global Claude or Codex CLI login into an arbitrary account
label.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import assert_never

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
from sidekick_usages.errors import UsageError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import Provider
from sidekick_usages.store import AccountStore

CLAUDE_REFRESH_MARGIN_SECONDS = 30 * 60
CODEX_REFRESH_MARGIN_SECONDS = 10 * 60


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


class TokenMaintenanceService:
    """Refresh saved provider tokens without adopting local logins."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[ProviderId, Provider],
        *,
        clock: Clock,
    ) -> None:
        """:param store: Account store to update after each attempt.

        :param http: Shared HTTP client passed to providers.
        :param providers: Provider registry.
        :param clock: Aware UTC application wall clock.
        """
        self.store = store
        self.http = http
        self.providers = providers
        self.clock = clock

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
        provider = self.providers.get(account.provider_id)
        if provider is None:
            return self._record_failed(
                account,
                f"Unknown provider '{account.provider_id}'.",
                exit_code=ExitCode.SYSTEM_ERROR,
            )

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

        try:
            refreshed = provider.refresh_token(account, self.http)
        except UsageError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=ExitCode.MANUAL_ACTION,
            )

        if not refreshed:
            return self._record_failed(
                account,
                "Refresh token unavailable or rejected.",
                exit_code=ExitCode.MANUAL_ACTION,
            )

        record_refresh_success(account, self.clock.now())
        self.store.upsert(account)
        self.store.save()
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
    ) -> RefreshOutcome:
        """Persist a failed refresh diagnostic and return its outcome."""
        record_refresh_failure(account, message, self.clock.now())
        self.store.upsert(account)
        self.store.save()
        return RefreshOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=RefreshStatus.FAILED,
            message=message,
            exit_code=exit_code,
            action_required=exit_code == ExitCode.MANUAL_ACTION,
        )


def refresh_margin_seconds(provider_id: ProviderId) -> int:
    """Return the provider-specific proactive refresh margin."""
    if provider_id == ProviderId.CLAUDE:
        return CLAUDE_REFRESH_MARGIN_SECONDS
    if provider_id == ProviderId.CODEX:
        return CODEX_REFRESH_MARGIN_SECONDS
    assert_never(provider_id)


def record_refresh_success(account: Account, reference_time: datetime) -> None:
    """Mark an account's latest refresh as successful."""
    account.last_refresh_at = reference_time
    account.last_refresh_status = RefreshStatus.OK
    account.last_refresh_error = None


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
