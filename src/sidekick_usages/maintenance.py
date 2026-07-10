"""Saved-token refresh maintenance.

This module owns the scheduler-safe refresh behavior. It only uses
credentials already stored in sidekick-usages; it never imports the
current global Claude or Codex CLI login into an arbitrary account
label.
"""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.errors import UsageError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import Provider
from sidekick_usages.store import Account, AccountStore

REFRESH_OK = "ok"
REFRESH_SKIPPED = "skipped"
REFRESH_FAILED = "failed"

CLAUDE_REFRESH_MARGIN_SECONDS = 30 * 60
CODEX_REFRESH_MARGIN_SECONDS = 10 * 60
EXIT_MANUAL_ACTION = 1
EXIT_SYSTEM_ERROR = 2


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of one saved-token maintenance refresh."""

    label: str
    provider_id: str
    status: str
    message: str
    exit_code: int = 0
    refreshed: bool = False
    action_required: bool = False


class TokenMaintenanceService:
    """Refresh saved provider tokens without adopting local logins."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[str, Provider],
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
        provider_id: str | None = None,
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
                exit_code=EXIT_SYSTEM_ERROR,
            )

        should_refresh, expiry_state = self._refresh_decision(
            account,
            force=force,
        )
        if not should_refresh:
            return RefreshOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=REFRESH_SKIPPED,
                message=expiry_state or "unknown",
            )

        if not account.refresh_token:
            return self._record_failed(
                account,
                "No refresh token saved; log in manually.",
                exit_code=EXIT_MANUAL_ACTION,
            )

        try:
            refreshed = provider.refresh_token(account, self.http)
        except UsageError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=EXIT_MANUAL_ACTION,
            )

        if not refreshed:
            return self._record_failed(
                account,
                "Refresh token unavailable or rejected.",
                exit_code=EXIT_MANUAL_ACTION,
            )

        record_refresh_success(account, self.clock.now())
        self.store.upsert(account)
        self.store.save()
        return RefreshOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=REFRESH_OK,
            message="refreshed",
            refreshed=True,
        )

    def should_refresh(self, account: Account, *, force: bool = False) -> bool:
        """Return whether maintenance should refresh this account."""
        should_refresh, _ = self._refresh_decision(account, force=force)
        return should_refresh

    def _refresh_decision(
        self,
        account: Account,
        *,
        force: bool,
    ) -> tuple[bool, str | None]:
        """Return one refresh decision and its sampled expiry state."""
        if force:
            return (True, None)
        expiry_state = self.expiry_state(account, self.clock.now())
        if not account.refresh_token:
            return (False, expiry_state)
        return (expiry_state in {"expired", "near_expiry"}, expiry_state)

    def expiry_state(
        self,
        account: Account,
        reference_time: datetime,
    ) -> str:
        """Classify account expiry relative to provider refresh margins."""
        expires_at = expiry_epoch_seconds(account)
        if expires_at is None:
            return "unknown"
        now = reference_time.timestamp()
        if expires_at <= now:
            return "expired"
        if expires_at <= now + refresh_margin_seconds(account.provider_id):
            return "near_expiry"
        return "fresh"

    def _record_failed(
        self,
        account: Account,
        message: str,
        *,
        exit_code: int,
    ) -> RefreshOutcome:
        """Persist a failed refresh diagnostic and return its outcome."""
        record_refresh_failure(account, message, self.clock.now())
        self.store.upsert(account)
        self.store.save()
        return RefreshOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=REFRESH_FAILED,
            message=message,
            exit_code=exit_code,
            action_required=exit_code == EXIT_MANUAL_ACTION,
        )


def refresh_margin_seconds(provider_id: str) -> int:
    """Return the provider-specific proactive refresh margin."""
    if provider_id == "claude":
        return CLAUDE_REFRESH_MARGIN_SECONDS
    if provider_id == "codex":
        return CODEX_REFRESH_MARGIN_SECONDS
    return CODEX_REFRESH_MARGIN_SECONDS


def expiry_epoch_seconds(account: Account) -> float | None:
    """Normalize provider-native expiry units to Unix seconds."""
    if account.expires_at is None:
        return None
    if account.provider_id == "claude":
        return account.expires_at / 1000
    return float(account.expires_at)


def record_refresh_success(account: Account, reference_time: datetime) -> None:
    """Mark an account's latest refresh as successful."""
    account.last_refresh_at = _utc_z(reference_time)
    account.last_refresh_status = REFRESH_OK
    account.last_refresh_error = None


def record_refresh_failure(
    account: Account,
    message: str,
    reference_time: datetime,
) -> None:
    """Mark an account's latest refresh as failed."""
    account.last_refresh_at = _utc_z(reference_time)
    account.last_refresh_status = REFRESH_FAILED
    account.last_refresh_error = message


def refresh_exit_code(outcomes: list[RefreshOutcome]) -> int:
    """Collapse per-account outcomes into the documented CLI exit code."""
    if any(outcome.exit_code == EXIT_SYSTEM_ERROR for outcome in outcomes):
        return EXIT_SYSTEM_ERROR
    if any(outcome.exit_code == EXIT_MANUAL_ACTION for outcome in outcomes):
        return EXIT_MANUAL_ACTION
    return 0


def _utc_z(value: datetime) -> str:
    """Return an aware timestamp in the existing UTC-Z representation."""
    return value.isoformat().replace("+00:00", "Z")
