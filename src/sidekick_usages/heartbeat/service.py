"""Policy and persistence for optional usage-window heartbeat."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from sidekick_usages.errors import UsageError
from sidekick_usages.heartbeat.base import HeartbeatProvider
from sidekick_usages.heartbeat.domain import (
    HEARTBEAT_ACTIVE,
    HEARTBEAT_DISABLED,
    HEARTBEAT_ENABLED,
    HEARTBEAT_FAILED,
    HEARTBEAT_UNSUPPORTED,
    HeartbeatOutcome,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import (
    EXIT_MANUAL_ACTION,
    EXIT_SYSTEM_ERROR,
    expiry_epoch_seconds,
)
from sidekick_usages.store import Account, AccountStore


class HeartbeatService:
    """Run and configure saved-account usage-window heartbeat."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: dict[str, HeartbeatProvider],
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.http = http
        self.providers = providers
        self._now = now or time.time

    def heartbeat_all(
        self,
        *,
        provider_id: str | None = None,
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

        ready = self._ready_provider(account)
        if isinstance(ready, HeartbeatOutcome):
            return ready
        provider = ready

        try:
            target = provider.resolve_target(account, target_id)
        except ValueError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=EXIT_SYSTEM_ERROR,
            )

        cached = _future_reset(
            _target_reset(account, target.id),
            self._now(),
        )
        if cached is not None:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HEARTBEAT_ACTIVE,
                message=f"{target.label} active until {cached}",
                target_id=target.id,
                target_label=target.label,
            )

        try:
            result = provider.run(account, self.http, target_id=target.id)
        except UsageError as e:
            return self._record_failed(
                account,
                str(e),
                exit_code=EXIT_MANUAL_ACTION,
            )

        account.last_heartbeat_at = _now_utc_z()
        account.last_heartbeat_status = result.status
        account.last_heartbeat_error = (
            result.message if result.status == HEARTBEAT_FAILED else None
        )
        if result.reset_at:
            _set_target_reset(account, target.id, result.reset_at)
        self.store.upsert(account)
        self.store.save()
        if result.status == HEARTBEAT_FAILED:
            exit_code = (
                EXIT_MANUAL_ACTION
                if result.action_required
                else EXIT_SYSTEM_ERROR
            )
        else:
            exit_code = EXIT_MANUAL_ACTION if result.action_required else 0
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
                status=HEARTBEAT_DISABLED,
                message="heartbeat disabled",
            )
        return None

    def _ready_provider(
        self,
        account: Account,
    ) -> HeartbeatProvider | HeartbeatOutcome:
        """Return a supported provider or the failure outcome to persist."""
        provider = self.providers.get(account.provider_id)
        if provider is None:
            return self._record_failed(
                account,
                f"Unknown provider '{account.provider_id}'.",
                exit_code=EXIT_SYSTEM_ERROR,
            )
        blocked = self._auth_blocker(account)
        if blocked is not None:
            return self._record_failed(
                account,
                blocked,
                exit_code=EXIT_MANUAL_ACTION,
            )
        if provider.supports(account):
            return provider
        return self._record_unsupported(account, provider)

    def enable(
        self,
        account: Account | None,
        *,
        target_id: str | None = None,
    ) -> HeartbeatOutcome:
        """Enable daemon heartbeat for one supported account."""
        if account is None:
            return _missing_account()
        provider = self.providers.get(account.provider_id)
        if provider is None:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HEARTBEAT_FAILED,
                message=f"Unknown provider '{account.provider_id}'.",
                action_required=True,
                exit_code=EXIT_SYSTEM_ERROR,
            )
        if not provider.supports(account):
            return self._unsupported_outcome(account, provider)
        try:
            selected = _selected_provider_targets(provider, account, target_id)
        except ValueError as e:
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HEARTBEAT_FAILED,
                message=str(e),
                action_required=False,
                exit_code=EXIT_SYSTEM_ERROR,
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
        self.store.upsert(account)
        self.store.save()
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HEARTBEAT_ENABLED,
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
            provider = self.providers.get(account.provider_id)
            if provider is None:
                return HeartbeatOutcome(
                    label=account.label,
                    provider_id=account.provider_id,
                    status=HEARTBEAT_FAILED,
                    message=f"Unknown provider '{account.provider_id}'.",
                    action_required=True,
                    exit_code=EXIT_SYSTEM_ERROR,
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
                    status=HEARTBEAT_FAILED,
                    message=str(e),
                    exit_code=EXIT_SYSTEM_ERROR,
                )
            current = account.heartbeat_targets or list(
                provider.default_target_ids(account)
            )
            account.heartbeat_targets = [
                item for item in current if item not in selected
            ]
            if not account.heartbeat_targets:
                account.heartbeat_enabled = False
                account.heartbeat_targets = None
            self.store.upsert(account)
            self.store.save()
            return HeartbeatOutcome(
                label=account.label,
                provider_id=account.provider_id,
                status=HEARTBEAT_DISABLED,
                message="disabled",
            )
        account.heartbeat_enabled = False
        self.store.upsert(account)
        self.store.save()
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HEARTBEAT_DISABLED,
            message="disabled",
        )

    def _auth_blocker(self, account: Account) -> str | None:
        """Return a user-action blocker for accounts that should not warm."""
        if account.last_refresh_status == "failed":
            return "Last token refresh failed; log in before heartbeat."
        expires_at = expiry_epoch_seconds(account)
        if expires_at is not None and expires_at <= self._now():
            return (
                "Access token is expired; refresh or log in before heartbeat."
            )
        return None

    def _record_unsupported(
        self,
        account: Account,
        provider: HeartbeatProvider,
    ) -> HeartbeatOutcome:
        outcome = self._unsupported_outcome(account, provider)
        account.last_heartbeat_at = _now_utc_z()
        account.last_heartbeat_status = HEARTBEAT_UNSUPPORTED
        account.last_heartbeat_error = outcome.message
        self.store.upsert(account)
        self.store.save()
        return outcome

    def _unsupported_outcome(
        self,
        account: Account,
        provider: HeartbeatProvider,
    ) -> HeartbeatOutcome:
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HEARTBEAT_UNSUPPORTED,
            message=provider.unsupported_message(account),
            action_required=True,
            exit_code=EXIT_MANUAL_ACTION,
        )

    def _record_failed(
        self,
        account: Account,
        message: str,
        *,
        exit_code: int,
    ) -> HeartbeatOutcome:
        account.last_heartbeat_at = _now_utc_z()
        account.last_heartbeat_status = HEARTBEAT_FAILED
        account.last_heartbeat_error = message
        self.store.upsert(account)
        self.store.save()
        return HeartbeatOutcome(
            label=account.label,
            provider_id=account.provider_id,
            status=HEARTBEAT_FAILED,
            message=message,
            action_required=exit_code == EXIT_MANUAL_ACTION,
            exit_code=exit_code,
        )

    def _selected_target_ids(
        self,
        account: Account,
        target_id: str | None,
    ) -> tuple[str | None, ...]:
        """Return target ids to process for one account."""
        provider = self.providers.get(account.provider_id)
        if provider is None:
            return (target_id,)
        if target_id is not None:
            return _selected_provider_targets(provider, account, target_id)
        if account.heartbeat_targets:
            return tuple(account.heartbeat_targets)
        return provider.default_target_ids(account)


def heartbeat_exit_code(outcomes: list[HeartbeatOutcome]) -> int:
    """Collapse per-account heartbeat outcomes to a CLI exit code."""
    if any(outcome.exit_code == EXIT_SYSTEM_ERROR for outcome in outcomes):
        return EXIT_SYSTEM_ERROR
    if any(outcome.exit_code == EXIT_MANUAL_ACTION for outcome in outcomes):
        return EXIT_MANUAL_ACTION
    return 0


def heartbeat_supported_label(
    account: Account,
    provider: HeartbeatProvider | None,
) -> str:
    """Return a compact heartbeat support label for list/doctor output."""
    if provider is None or not provider.supports(account):
        return "unsupported"
    if account.last_heartbeat_status == HEARTBEAT_FAILED:
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
) -> list[str]:
    """Merge selected targets with current/default account targets in provider order."""
    current = account.heartbeat_targets or list(
        provider.default_target_ids(account)
    )
    wanted = set(current)
    wanted.update(selected)
    return [
        target.id
        for target in provider.supported_targets(account)
        if target.id in wanted
    ]


def _target_reset(account: Account, target_id: str) -> str | None:
    """Return a cached reset for one target, with legacy field fallback."""
    if account.heartbeat_window_resets:
        value = account.heartbeat_window_resets.get(target_id)
        if value:
            return value
    if target_id == "standard":
        return account.heartbeat_5h_reset_at
    return None


def _set_target_reset(account: Account, target_id: str, reset_at: str) -> None:
    """Persist one target reset and keep the legacy standard field current."""
    resets = dict(account.heartbeat_window_resets or {})
    resets[target_id] = reset_at
    account.heartbeat_window_resets = resets
    if target_id == "standard":
        account.heartbeat_5h_reset_at = reset_at


def _missing_account() -> HeartbeatOutcome:
    """Return a stable missing-account outcome."""
    return HeartbeatOutcome(
        label="?",
        provider_id="unknown",
        status=HEARTBEAT_FAILED,
        message="Account not found.",
        exit_code=EXIT_SYSTEM_ERROR,
    )


def _future_reset(value: str | None, now: float) -> str | None:
    """Return ``value`` when it parses to a future timestamp."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        reset_at = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=UTC)
    if reset_at.timestamp() > now:
        return value
    return None


def _now_utc_z() -> str:
    """Return an ISO UTC timestamp with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
