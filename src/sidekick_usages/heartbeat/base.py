"""Abstract provider adapter for usage-window heartbeat."""

from abc import ABC, abstractmethod

from sidekick_usages.heartbeat.domain import (
    HEARTBEAT_ACTIVE,
    HEARTBEAT_WARMED,
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.store import Account

STANDARD_HEARTBEAT_TARGET = HeartbeatTarget(
    id="standard",
    label="5h",
    default=True,
)


class HeartbeatProvider(ABC):
    """Template for provider-specific usage-window warming.

    The service owns policy and persistence. Subclasses own provider
    protocol details: whether the account is supported, how to inspect
    the current window, and how to send the smallest valid request.
    """

    id: str = ""
    display_name: str = ""

    @abstractmethod
    def supports(self, account: Account) -> bool:
        """Return whether this account can be warmed safely."""

    def supported_targets(
        self, account: Account
    ) -> tuple[HeartbeatTarget, ...]:
        """Return usage windows this provider can warm for ``account``."""
        if not self.supports(account):
            return ()
        return (STANDARD_HEARTBEAT_TARGET,)

    def default_target_ids(self, account: Account) -> tuple[str, ...]:
        """Return target ids used when no target is requested explicitly."""
        targets = self.supported_targets(account)
        defaults = tuple(target.id for target in targets if target.default)
        if defaults:
            return defaults
        return (targets[0].id,) if targets else ()

    def resolve_target(
        self,
        account: Account,
        target_id: str | None,
    ) -> HeartbeatTarget:
        """Resolve a requested target id to a supported target."""
        targets = self.supported_targets(account)
        if not targets:
            raise ValueError(self.unsupported_message(account))
        if target_id is None:
            default_ids = self.default_target_ids(account)
            target_id = default_ids[0] if default_ids else targets[0].id
        for target in targets:
            if target.id == target_id:
                return target
        supported = ", ".join(target.id for target in targets)
        raise ValueError(
            f"Unsupported heartbeat target '{target_id}' for "
            f"'{account.label}'. Supported targets: {supported}."
        )

    def unsupported_message(self, account: Account) -> str:
        """Return user-facing unsupported detail."""
        return f"Heartbeat is not supported for '{account.label}'."

    def run(
        self,
        account: Account,
        http: HttpClient,
        *,
        target_id: str | None = None,
    ) -> HeartbeatProbeResult:
        """Inspect the window and warm it if inactive."""
        target = self.resolve_target(account, target_id)
        state = self.inspect_window(account, http, target)
        if state.active:
            return HeartbeatProbeResult(
                status=HEARTBEAT_ACTIVE,
                message=state.message,
                warmed=False,
                reset_at=state.reset_at,
                target_id=target.id,
                target_label=target.label,
            )
        result = self.warm_window(account, http, target)
        return _with_target(result, target)

    @abstractmethod
    def inspect_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        """Read provider state without sending a warming request if possible."""

    @abstractmethod
    def warm_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Send the provider's smallest valid warming request."""


def warmed(
    reset_at: str | None,
    target: HeartbeatTarget | None = None,
) -> HeartbeatProbeResult:
    """Build a standard warmed result."""
    return HeartbeatProbeResult(
        status=HEARTBEAT_WARMED,
        message="warmed",
        warmed=True,
        reset_at=reset_at,
        target_id=target.id if target else None,
        target_label=target.label if target else None,
    )


def _with_target(
    result: HeartbeatProbeResult,
    target: HeartbeatTarget,
) -> HeartbeatProbeResult:
    """Attach target metadata when a provider helper omitted it."""
    if result.target_id and result.target_label:
        return result
    return HeartbeatProbeResult(
        status=result.status,
        message=result.message,
        warmed=result.warmed,
        reset_at=result.reset_at,
        action_required=result.action_required,
        target_id=result.target_id or target.id,
        target_label=result.target_label or target.label,
    )
