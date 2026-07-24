"""Policy and persistence for optional usage-window heartbeat."""

from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialResolver,
)
from sidekick_usages.errors import UsageError
from sidekick_usages.heartbeat.models import (
    HeartbeatOutcome,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_runtime_bridge import (
    saved_account_from_runtime_state,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError


class HeartbeatService:
    """Run and configure saved-account usage-window heartbeat."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[ProviderId, HeartbeatProvider],
        *,
        clock: Clock,
        resolver: CredentialResolver,
    ) -> None:
        self.store = store
        self.http = http
        self._providers = dict(providers)
        self.clock = clock
        self._resolver = resolver

    def support_label(self, account: Account) -> str:
        """Return display-ready support state without exposing adapters."""
        return heartbeat_supported_label(
            account,
            self._providers.get(account.provider_id),
        )

    def support_labels(
        self,
        accounts: Iterable[Account],
    ) -> dict[AccountLabel, str]:
        """Return support state keyed by exact account label."""
        return {
            account.label: self.support_label(account) for account in accounts
        }

    def heartbeat_all(
        self,
        *,
        provider_id: ProviderId | None = None,
        target_id: str | None = None,
    ) -> list[HeartbeatOutcome]:
        """Heartbeat every enabled matching account."""
        accounts = list(self.store)
        if provider_id is not None:
            accounts = [a for a in accounts if a.provider_id == provider_id]
        outcomes: list[HeartbeatOutcome] = []
        for account in accounts:
            for selected in self._selected_target_ids(account, target_id):
                outcomes.append(
                    self.heartbeat_account(
                        account,
                        require_enabled=True,
                        target_id=selected,
                    )
                )
        return outcomes

    def heartbeat_account(
        self,
        account: Account | None,
        *,
        require_enabled: bool = True,
        target_id: str | None = None,
    ) -> HeartbeatOutcome:
        """Run one heartbeat, respecting opt-in unless explicitly bypassed."""
        early = self._early_account_outcome(account, require_enabled)
        if early is not None:
            return early
        assert account is not None
        reference_time = self.clock.now()

        ready = self._ready_provider(account, reference_time)
        if isinstance(ready, HeartbeatOutcome):
            return ready
        provider = ready

        try:
            target = provider.resolve_target(account, target_id)
        except ValueError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=ExitCode.SYSTEM_ERROR,
                reference_time=reference_time,
            )

        cached = _future_reset(
            _target_reset(account, target.id),
            reference_time,
        )
        if cached is not None:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HeartbeatStatus.ACTIVE,
                message=f"{target.label} active until {cached.isoformat()}",
                target_id=target.id,
                target_label=target.label,
            )

        try:
            with self._open_account(account) as authenticated:
                result = provider.run(
                    authenticated,
                    self.http,
                    target_id=target.id,
                )
        except UsageError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=ExitCode.MANUAL_ACTION,
                reference_time=self.clock.now(),
            )

        account.last_heartbeat_at = self.clock.now()
        account.last_heartbeat_status = result.status
        account.last_heartbeat_error = (
            result.message if result.status is HeartbeatStatus.FAILED else None
        )
        if result.reset_at:
            _set_target_reset(account, target.id, result.reset_at)
        self._persist_state(account)
        if result.status is HeartbeatStatus.FAILED:
            exit_code = (
                ExitCode.MANUAL_ACTION
                if result.action_required
                else ExitCode.SYSTEM_ERROR
            )
        else:
            exit_code = (
                ExitCode.MANUAL_ACTION
                if result.action_required
                else ExitCode.SUCCESS
            )
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=result.status,
            message=result.message,
            warmed=result.warmed,
            action_required=result.action_required,
            exit_code=exit_code,
            target_id=result.target_id,
            target_label=result.target_label,
        )

    def _open_account(
        self,
        account: Account,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open one heartbeat credential lease at the provider boundary."""
        saved = self._saved_account(account)
        if saved is None:
            raise UsageError("The heartbeat account changed.")
        return self._resolver.open(saved)

    def _persist_state(self, account: Account) -> None:
        """Persist status without carrying credentials into the v3 index."""
        saved = self._saved_account(account)
        if saved is None:
            raise SourceChangedError
        self.store.persist_state(
            saved_account_from_runtime_state(saved, account),
            expected=saved,
        )

    def _saved_account(self, account: Account) -> SavedAccount | None:
        """Return exact stable metadata for one runtime view."""
        return next(
            (
                candidate
                for candidate in self.store.saved_accounts()
                if candidate.provider_id is account.provider_id
                and candidate.label == account.label
            ),
            None,
        )

    def _early_account_outcome(
        self,
        account: Account | None,
        require_enabled: bool,
    ) -> HeartbeatOutcome | None:
        """Return an account-level skip/failure before provider lookup."""
        if account is None:
            return _missing_account()
        if require_enabled and not account.heartbeat_enabled:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HeartbeatStatus.DISABLED,
                message="heartbeat disabled",
            )
        return None

    def _ready_provider(
        self,
        account: Account,
        reference_time: datetime,
    ) -> HeartbeatProvider | HeartbeatOutcome:
        """Return a supported provider or the failure outcome to persist."""
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return self._record_failed(
                account,
                f"Unknown provider '{account.provider_id}'.",
                exit_code=ExitCode.SYSTEM_ERROR,
                reference_time=reference_time,
            )
        blocked = self._auth_blocker(account, reference_time)
        if blocked is not None:
            return self._record_failed(
                account,
                blocked,
                exit_code=ExitCode.MANUAL_ACTION,
                reference_time=reference_time,
            )
        if provider.supports(account):
            return provider
        return self._record_unsupported(account, provider, reference_time)

    def enable(
        self,
        account: Account | None,
        *,
        target_id: str | None = None,
    ) -> HeartbeatOutcome:
        """Enable daemon heartbeat for one supported account."""
        if account is None:
            return _missing_account()
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HeartbeatStatus.FAILED,
                message=f"Unknown provider '{account.provider_id}'.",
                action_required=True,
                exit_code=ExitCode.SYSTEM_ERROR,
            )
        if not provider.supports(account):
            return self._unsupported_outcome(account, provider)
        try:
            selected = _selected_provider_targets(provider, account, target_id)
        except ValueError as e:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HeartbeatStatus.FAILED,
                message=str(e),
                action_required=False,
                exit_code=ExitCode.SYSTEM_ERROR,
            )
        account.heartbeat_enabled = True
        if target_id is None:
            account.heartbeat_targets = None
        else:
            account.heartbeat_targets = _merge_targets(
                provider,
                account,
                selected,
            )
        self._persist_state(account)
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HeartbeatStatus.ENABLED,
            message="enabled",
        )

    def disable(
        self,
        account: Account | None,
        *,
        target_id: str | None = None,
    ) -> HeartbeatOutcome:
        """Disable daemon heartbeat for one account."""
        if account is None:
            return _missing_account()
        if target_id is not None:
            provider = self._providers.get(account.provider_id)
            if provider is None:
                return HeartbeatOutcome(
                    label=account.label,
                    provider_id=account.provider_id,
                    status=HeartbeatStatus.FAILED,
                    message=f"Unknown provider '{account.provider_id}'.",
                    action_required=True,
                    exit_code=ExitCode.SYSTEM_ERROR,
                )
            try:
                selected = _selected_provider_targets(
                    provider,
                    account,
                    target_id,
                )
            except ValueError as e:
                return HeartbeatOutcome(
                    label=account.label,
                    provider_id=account.provider_id,
                    status=HeartbeatStatus.FAILED,
                    message=str(e),
                    exit_code=ExitCode.SYSTEM_ERROR,
                )
            current = account.heartbeat_targets or list(
                provider.default_target_ids(account)
            )
            account.heartbeat_targets = tuple(
                item for item in current if item not in selected
            )
            if not account.heartbeat_targets:
                account.heartbeat_enabled = False
                account.heartbeat_targets = None
            self._persist_state(account)
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HeartbeatStatus.DISABLED,
                message="disabled",
            )
        account.heartbeat_enabled = False
        self._persist_state(account)
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HeartbeatStatus.DISABLED,
            message="disabled",
        )

    def _auth_blocker(
        self,
        account: Account,
        reference_time: datetime,
    ) -> str | None:
        """Return a user-action blocker for accounts that should not warm."""
        if account.last_refresh_status is RefreshStatus.FAILED:
            if isinstance(account.credentials, ClaudeSetupTokenCredentials):
                return account.last_refresh_error or (
                    "Last setup-token check failed; replace the token before "
                    "heartbeat."
                )
            return "Last token refresh failed; log in before heartbeat."
        expiry = classify_expiry(account.expiry, now=reference_time)
        if isinstance(expiry, InvalidExpiry):
            return "Access-token expiry metadata is invalid; log in again."
        if isinstance(expiry, ExpiredExpiry):
            return (
                "Access token is expired; refresh or log in before heartbeat."
            )
        return None

    def _record_unsupported(
        self,
        account: Account,
        provider: HeartbeatProvider,
        reference_time: datetime,
    ) -> HeartbeatOutcome:
        outcome = self._unsupported_outcome(account, provider)
        account.last_heartbeat_at = reference_time
        account.last_heartbeat_status = HeartbeatStatus.UNSUPPORTED
        account.last_heartbeat_error = outcome.message
        self._persist_state(account)
        return outcome

    def _unsupported_outcome(
        self,
        account: Account,
        provider: HeartbeatProvider,
    ) -> HeartbeatOutcome:
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HeartbeatStatus.UNSUPPORTED,
            message=provider.unsupported_message(account),
            action_required=True,
            exit_code=ExitCode.MANUAL_ACTION,
        )

    def _record_failed(
        self,
        account: Account,
        message: str,
        *,
        exit_code: ExitCode,
        reference_time: datetime,
    ) -> HeartbeatOutcome:
        account.last_heartbeat_at = reference_time
        account.last_heartbeat_status = HeartbeatStatus.FAILED
        account.last_heartbeat_error = message
        self._persist_state(account)
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HeartbeatStatus.FAILED,
            message=message,
            action_required=exit_code == ExitCode.MANUAL_ACTION,
            exit_code=exit_code,
        )

    def _selected_target_ids(
        self,
        account: Account,
        target_id: str | None,
    ) -> tuple[str | None, ...]:
        """Return target ids to process for one account."""
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return (target_id,)
        if target_id is not None:
            return _selected_provider_targets(provider, account, target_id)
        if account.heartbeat_targets:
            return tuple(account.heartbeat_targets)
        return provider.default_target_ids(account)


def heartbeat_exit_code(outcomes: list[HeartbeatOutcome]) -> ExitCode:
    """Collapse per-account heartbeat outcomes to a CLI exit code."""
    if any(outcome.exit_code == ExitCode.SYSTEM_ERROR for outcome in outcomes):
        return ExitCode.SYSTEM_ERROR
    if any(
        outcome.exit_code == ExitCode.MANUAL_ACTION for outcome in outcomes
    ):
        return ExitCode.MANUAL_ACTION
    return ExitCode.SUCCESS


def heartbeat_supported_label(
    account: Account,
    provider: HeartbeatProvider | None,
) -> str:
    """Return a compact heartbeat support label for list/doctor output."""
    if provider is None or not provider.supports(account):
        return "unsupported"
    if account.last_heartbeat_status is HeartbeatStatus.FAILED:
        return "needs-login"
    return "on" if account.heartbeat_enabled else "off"


def _selected_provider_targets(
    provider: HeartbeatProvider,
    account: Account,
    target_id: str | None,
) -> tuple[str, ...]:
    """Resolve one target selector into concrete target ids."""
    if target_id == "all":
        return tuple(
            target.id for target in provider.supported_targets(account)
        )
    if target_id is None:
        return provider.default_target_ids(account)
    return (provider.resolve_target(account, target_id).id,)


def _merge_targets(
    provider: HeartbeatProvider,
    account: Account,
    selected: tuple[str, ...],
) -> tuple[str, ...]:
    """Merge selected with configured/default targets in provider order."""
    current = account.heartbeat_targets or list(
        provider.default_target_ids(account)
    )
    wanted = set(current)
    wanted.update(selected)
    return tuple(
        target.id
        for target in provider.supported_targets(account)
        if target.id in wanted
    )


def _target_reset(account: Account, target_id: str) -> datetime | None:
    """Return the cached reset for one target."""
    if not account.heartbeat_window_resets:
        return None
    return account.heartbeat_window_resets.get(target_id)


def _set_target_reset(
    account: Account,
    target_id: str,
    reset_at: datetime,
) -> None:
    """Persist one target reset."""
    resets = dict(account.heartbeat_window_resets or {})
    resets[target_id] = reset_at
    account.heartbeat_window_resets = resets


def _missing_account() -> HeartbeatOutcome:
    """Return a stable missing-account outcome."""
    return HeartbeatOutcome(
        label=None,
        provider_id=None,
        status=HeartbeatStatus.FAILED,
        message="Account not found.",
        exit_code=ExitCode.SYSTEM_ERROR,
    )


def _future_reset(
    reset_at: datetime | None,
    reference_time: datetime,
) -> datetime | None:
    """Return ``reset_at`` only while it remains in the future."""
    if reset_at is not None and reset_at > reference_time:
        return reset_at
    return None
