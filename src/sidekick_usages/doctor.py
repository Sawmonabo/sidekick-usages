"""Read-only account diagnostics for ``sidekick-usages doctor``."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from rich.console import Console

from sidekick_usages.maintenance import (
    TokenMaintenanceService,
    expiry_epoch_seconds,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import PROFILE_SCOPE
from sidekick_usages.store import Account, AccountStore

_IDENTITY_FULL_MAX_LENGTH = 12


@dataclass(frozen=True)
class AccountDiagnostic:
    """Public doctor data for one account."""

    label: str
    provider: str
    plan: str
    usage_route: str
    has_refresh_token: bool
    expires_at: int | None
    expires_at_local: str | None
    identity_fingerprint: str | None
    can_auto_refresh: bool
    expiry_state: str
    last_refresh_at: str | None
    last_refresh_status: str | None
    last_refresh_error: str | None
    manual_action_required: bool


class DoctorService:
    """Build and render read-only app health diagnostics."""

    def __init__(
        self,
        store: AccountStore,
        providers: dict[str, Provider],
        maintenance: TokenMaintenanceService,
    ) -> None:
        """:param store: Account store to inspect.

        :param providers: Registered provider map.
        :param maintenance: Refresh policy service for expiry state.
        """
        self.store = store
        self.providers = providers
        self.maintenance = maintenance

    def diagnostics(
        self,
        *,
        provider_id: str | None = None,
        label: str | None = None,
    ) -> list[AccountDiagnostic]:
        """Return diagnostics for accounts matching optional filters."""
        accounts = list(self.store)
        if provider_id is not None:
            accounts = [a for a in accounts if a.provider_id == provider_id]
        if label is not None:
            accounts = [a for a in accounts if a.label == label]
        return [self._diagnostic(account) for account in accounts]

    def _diagnostic(self, account: Account) -> AccountDiagnostic:
        """Build one account diagnostic."""
        provider = self.providers.get(account.provider_id)
        expiry_state = self.maintenance.expiry_state(account)
        can_auto_refresh = bool(provider and account.refresh_token)
        manual_action_required = _manual_action_required(
            account,
            can_auto_refresh=can_auto_refresh,
            expiry_state=expiry_state,
            provider_known=provider is not None,
        )
        return AccountDiagnostic(
            label=account.label,
            provider=account.provider_id,
            plan=account.plan,
            usage_route=usage_route(account),
            has_refresh_token=bool(account.refresh_token),
            expires_at=account.expires_at,
            expires_at_local=_expires_at_local(account),
            identity_fingerprint=_identity_fingerprint(account),
            can_auto_refresh=can_auto_refresh,
            expiry_state=expiry_state,
            last_refresh_at=account.last_refresh_at,
            last_refresh_status=account.last_refresh_status,
            last_refresh_error=account.last_refresh_error,
            manual_action_required=manual_action_required,
        )


def usage_route(account: Account) -> str:
    """Return the provider route sidekick-usages will use for usage."""
    if account.provider_id == "claude":
        if account.scopes is not None and PROFILE_SCOPE not in account.scopes:
            return "/v1/messages headers"
        return "/api/oauth/usage"
    if account.provider_id == "codex":
        return "/backend-api/codex/usage"
    return "unknown"


def render_doctor(
    diagnostics: list[AccountDiagnostic],
    console: Console,
    *,
    json_output: bool = False,
) -> None:
    """Render doctor diagnostics to the configured console."""
    if json_output:
        console.print(
            json.dumps(
                {"accounts": [asdict(d) for d in diagnostics]},
                indent=2,
            )
        )
        return

    for index, diagnostic in enumerate(diagnostics):
        if index:
            console.print()
        suffix = (
            f" · {diagnostic.plan}" if diagnostic.plan != "unknown" else ""
        )
        console.print(f"{diagnostic.label}  [{diagnostic.provider}{suffix}]")
        console.print(f"  usage route: {diagnostic.usage_route}")
        console.print(
            "  refresh token: "
            + ("present" if diagnostic.has_refresh_token else "none")
        )
        console.print(
            "  auto-refresh: "
            + ("yes" if diagnostic.can_auto_refresh else "no")
        )
        if diagnostic.expires_at_local:
            console.print(f"  expires: {diagnostic.expires_at_local}")
        else:
            console.print("  expires: unknown")
        if diagnostic.identity_fingerprint:
            console.print(f"  identity: {diagnostic.identity_fingerprint}")
        if diagnostic.last_refresh_status:
            console.print(f"  last refresh: {diagnostic.last_refresh_status}")
        if diagnostic.last_refresh_error:
            console.print(f"  error: {diagnostic.last_refresh_error}")
        console.print(
            "  manual action: "
            + ("yes" if diagnostic.manual_action_required else "no")
        )


def doctor_exit_code(diagnostics: list[AccountDiagnostic]) -> int:
    """Return 1 when doctor found an account needing manual action."""
    return 1 if any(d.manual_action_required for d in diagnostics) else 0


def _manual_action_required(
    account: Account,
    *,
    can_auto_refresh: bool,
    expiry_state: str,
    provider_known: bool,
) -> bool:
    """Return whether the user needs to log in or fix config."""
    if not provider_known:
        return True
    if account.last_refresh_status == "failed":
        return True
    return expiry_state == "expired" and not can_auto_refresh


def _expires_at_local(account: Account) -> str | None:
    """Render expiry as a local ISO timestamp."""
    expires_at = expiry_epoch_seconds(account)
    if expires_at is None:
        return None
    return datetime.fromtimestamp(expires_at).astimezone().isoformat()


def _identity_fingerprint(account: Account) -> str | None:
    """Return a short provider identity fingerprint, never a token."""
    value = account.provider_account_id
    if not value:
        return None
    if len(value) <= _IDENTITY_FULL_MAX_LENGTH:
        return value
    return f"{value[:8]}…{value[-4:]}"


def diagnostic_dicts(
    diagnostics: list[AccountDiagnostic],
) -> list[dict[str, Any]]:
    """Expose diagnostics as plain dicts for tests or future callers."""
    return [asdict(diagnostic) for diagnostic in diagnostics]
