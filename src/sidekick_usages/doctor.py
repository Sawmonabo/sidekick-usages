"""Read-only account diagnostics for ``sidekick-usages doctor``."""

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Console

from sidekick_usages.branding import brand_header
from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ClassifiedExpiry,
    ExpiredExpiry,
    InvalidExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ExpiryState,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.heartbeat import (
    HeartbeatProvider,
    heartbeat_supported_label,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import PROFILE_SCOPE

_IDENTITY_FULL_MAX_LENGTH = 12


@dataclass(frozen=True)
class AccountDiagnostic:
    """Public doctor data for one account."""

    label: AccountLabel
    provider: ProviderId
    plan: str
    usage_route: str
    has_refresh_token: bool
    expires_at: datetime | None
    expires_at_local: str | None
    identity_fingerprint: str | None
    can_auto_refresh: bool
    expiry_state: ExpiryState
    last_refresh_at: datetime | None
    last_refresh_status: RefreshStatus | None
    last_refresh_error: str | None
    heartbeat_supported: bool
    heartbeat_enabled: bool
    heartbeat: str
    heartbeat_5h_reset_at: datetime | None
    heartbeat_window_resets: Mapping[str, datetime] | None
    heartbeat_targets: tuple[str, ...] | None
    last_heartbeat_at: datetime | None
    last_heartbeat_status: HeartbeatStatus | None
    last_heartbeat_error: str | None
    manual_action_required: bool


class DoctorService:
    """Build and render read-only app health diagnostics."""

    def __init__(
        self,
        accounts: Sequence[Account],
        providers: dict[ProviderId, Provider],
        heartbeat_providers: dict[ProviderId, HeartbeatProvider],
        clock: Clock,
    ) -> None:
        """:param accounts: Validated read-only account snapshot.

        :param providers: Registered provider map.
        :param heartbeat_providers: Registered heartbeat provider map.
        :param clock: Aware UTC application wall clock.
        """
        self.accounts = tuple(accounts)
        self.providers = providers
        self.heartbeat_providers = heartbeat_providers
        self.clock = clock

    def diagnostics(
        self,
        *,
        provider_id: ProviderId | None = None,
        label: str | None = None,
    ) -> list[AccountDiagnostic]:
        """Return diagnostics for accounts matching optional filters."""
        accounts = list(self.accounts)
        if provider_id is not None:
            accounts = [a for a in accounts if a.provider_id == provider_id]
        if label is not None:
            accounts = [a for a in accounts if a.label == label]
        reference_time = self.clock.now()
        return [
            self._diagnostic(account, reference_time) for account in accounts
        ]

    def _diagnostic(
        self,
        account: Account,
        reference_time: datetime,
    ) -> AccountDiagnostic:
        """Build one account diagnostic."""
        provider = self.providers.get(account.provider_id)
        heartbeat_provider = self.heartbeat_providers.get(account.provider_id)
        expiry = classify_expiry(account.expiry, now=reference_time)
        can_auto_refresh = bool(provider and account.refresh_token)
        manual_action_required = _manual_action_required(
            account,
            can_auto_refresh=can_auto_refresh,
            expiry=expiry,
            provider_known=provider is not None,
        )
        return AccountDiagnostic(
            label=account.label,
            provider=account.provider_id,
            plan=account.plan,
            usage_route=usage_route(account),
            has_refresh_token=bool(account.refresh_token),
            expires_at=_expiry_time(expiry),
            expires_at_local=_expires_at_local(expiry),
            identity_fingerprint=_identity_fingerprint(account),
            can_auto_refresh=can_auto_refresh,
            expiry_state=expiry.state,
            last_refresh_at=account.last_refresh_at,
            last_refresh_status=account.last_refresh_status,
            last_refresh_error=account.last_refresh_error,
            heartbeat_supported=bool(
                heartbeat_provider and heartbeat_provider.supports(account)
            ),
            heartbeat_enabled=account.heartbeat_enabled,
            heartbeat=heartbeat_supported_label(account, heartbeat_provider),
            heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
            heartbeat_window_resets=account.heartbeat_window_resets,
            heartbeat_targets=account.heartbeat_targets,
            last_heartbeat_at=account.last_heartbeat_at,
            last_heartbeat_status=account.last_heartbeat_status,
            last_heartbeat_error=account.last_heartbeat_error,
            manual_action_required=manual_action_required,
        )


def usage_route(account: Account) -> str:
    """Return the provider route sidekick-usages will use for usage."""
    if account.provider_id == ProviderId.CLAUDE:
        if account.scopes is not None and PROFILE_SCOPE not in account.scopes:
            return "/v1/messages headers"
        return "/api/oauth/usage"
    if account.provider_id == ProviderId.CODEX:
        return "/backend-api/codex/usage"
    return "unknown"


def render_doctor(
    diagnostics: list[AccountDiagnostic],
    console: Console,
    *,
    json_output: bool = False,
    persistence: PersistenceAssessment | None = None,
    persistence_failure: PersistenceCompositionFailure | None = None,
) -> None:
    """Render doctor diagnostics to the configured console."""
    if persistence is not None and persistence_failure is not None:
        raise ValueError("Doctor accepts one persistence result.")
    if json_output:
        payload: dict[str, object] = {
            "accounts": [
                _diagnostic_dict(diagnostic) for diagnostic in diagnostics
            ]
        }
        if persistence is not None:
            payload["persistence"] = _persistence_dict(persistence)
        elif persistence_failure is not None:
            payload["persistence"] = _persistence_failure_dict(
                persistence_failure
            )
        console.print(
            json.dumps(payload, indent=2),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return

    console.print(
        brand_header(
            console.size.width,
            section="doctor · account diagnostics",
        )
    )
    if persistence is not None:
        _render_persistence(persistence, console)
        if diagnostics:
            console.print()
    elif persistence_failure is not None:
        _render_persistence_failure(persistence_failure, console)
    for index, diagnostic in enumerate(diagnostics):
        if index:
            console.print()
        suffix = (
            f" · {diagnostic.plan}" if diagnostic.plan != "unknown" else ""
        )
        console.print(f"{diagnostic.label}  [{diagnostic.provider}{suffix}]")
        _render_auth_diagnostic(diagnostic, console)
        _render_heartbeat_diagnostic(diagnostic, console)
        _render_manual_action(diagnostic, console)


def _render_persistence(
    assessment: PersistenceAssessment,
    console: Console,
) -> None:
    """Render one safe frozen persistence assessment."""
    console.print("persistence")
    console.print(f"  state: {assessment.code}")
    console.print(f"  generation: {assessment.generation}")
    console.print(f"  path: {assessment.safe_path}")
    count = (
        str(assessment.account_count)
        if assessment.account_count is not None
        else "unknown"
    )
    console.print(f"  validated accounts: {count}")
    console.print(f"  message: {assessment.message}")
    if assessment.artifact_basename is not None:
        console.print(f"  artifact: {assessment.artifact_basename}")
    if assessment.next_command is not None:
        console.print("  next: " + " ".join(assessment.next_command))
    if assessment.guidance is not None:
        console.print(f"  guidance: {assessment.guidance}")
    if len(assessment.issues) > 1:
        console.print("  additional findings:")
        for issue in assessment.issues[1:]:
            artifact = (
                f" ({issue.artifact_basename})"
                if issue.artifact_basename is not None
                else ""
            )
            console.print(f"    {issue.code}{artifact}: {issue.message}")


def _persistence_dict(
    assessment: PersistenceAssessment,
) -> dict[str, object]:
    """Build one secret-free machine-readable persistence record."""
    return {
        "code": assessment.code.value,
        "generation": assessment.generation.value,
        "schema_version": assessment.schema_version,
        "account_count": assessment.account_count,
        "safe_path": str(assessment.safe_path),
        "artifact_basename": assessment.artifact_basename,
        "write_blocked": assessment.write_blocked,
        "next_command": list(assessment.next_command)
        if assessment.next_command is not None
        else None,
        "message": assessment.message,
        "guidance": assessment.guidance,
        "issues": [
            {
                "code": issue.code.value,
                "artifact_basename": issue.artifact_basename,
                "message": issue.message,
            }
            for issue in assessment.issues
        ],
    }


def _render_persistence_failure(
    failure: PersistenceCompositionFailure,
    console: Console,
) -> None:
    """Render one safe passive composition failure."""
    console.print("persistence")
    console.print(f"  state: {failure.code}")
    console.print(f"  path: {failure.safe_path}")
    console.print(f"  message: {failure.message}")
    if failure.artifact_basename is not None:
        console.print(f"  artifact: {failure.artifact_basename}")
    if failure.guidance is not None:
        console.print(f"  guidance: {failure.guidance}")
    if failure.next_command is not None:
        console.print("  next: " + shlex.join(failure.next_command))


def _persistence_failure_dict(
    failure: PersistenceCompositionFailure,
) -> dict[str, object]:
    """Build one secret-free machine-readable composition failure."""
    return {
        "code": failure.code.value,
        "generation": "unknown",
        "schema_version": None,
        "account_count": None,
        "safe_path": str(failure.safe_path),
        "artifact_basename": failure.artifact_basename,
        "write_blocked": True,
        "next_command": list(failure.next_command)
        if failure.next_command is not None
        else None,
        "message": failure.message,
        "guidance": failure.guidance,
        "issues": [
            {
                "code": failure.code.value,
                "artifact_basename": failure.artifact_basename,
                "message": failure.message,
            }
        ],
    }


def _render_auth_diagnostic(
    diagnostic: AccountDiagnostic,
    console: Console,
) -> None:
    """Render auth and refresh status for one account."""
    console.print(f"  usage route: {diagnostic.usage_route}")
    console.print(
        "  refresh token: "
        + ("present" if diagnostic.has_refresh_token else "none")
    )
    console.print(
        "  auto-refresh: " + ("yes" if diagnostic.can_auto_refresh else "no")
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


def _render_heartbeat_diagnostic(
    diagnostic: AccountDiagnostic,
    console: Console,
) -> None:
    """Render heartbeat status for one account."""
    console.print(
        "  heartbeat supported: "
        + ("yes" if diagnostic.heartbeat_supported else "no")
    )
    console.print(f"  heartbeat: {diagnostic.heartbeat}")
    console.print(
        "  heartbeat enabled: "
        + ("yes" if diagnostic.heartbeat_enabled else "no")
    )
    if diagnostic.heartbeat_5h_reset_at:
        console.print(
            "  cached 5h reset: "
            + _format_machine_time(diagnostic.heartbeat_5h_reset_at)
        )
    if diagnostic.heartbeat_window_resets:
        for target_id, reset_at in diagnostic.heartbeat_window_resets.items():
            console.print(
                f"  cached {target_id} reset: {_format_machine_time(reset_at)}"
            )
    if diagnostic.heartbeat_targets:
        console.print(
            "  heartbeat targets: " + ", ".join(diagnostic.heartbeat_targets)
        )
    if diagnostic.last_heartbeat_status:
        console.print(f"  last heartbeat: {diagnostic.last_heartbeat_status}")
    if diagnostic.last_heartbeat_error:
        console.print(f"  heartbeat error: {diagnostic.last_heartbeat_error}")


def _render_manual_action(
    diagnostic: AccountDiagnostic,
    console: Console,
) -> None:
    """Render manual-action summary for one account."""
    console.print(
        "  manual action: "
        + ("yes" if diagnostic.manual_action_required else "no")
    )


def doctor_exit_code(diagnostics: list[AccountDiagnostic]) -> ExitCode:
    """Return 1 when doctor found an account needing manual action."""
    if any(d.manual_action_required for d in diagnostics):
        return ExitCode.MANUAL_ACTION
    return ExitCode.SUCCESS


def _manual_action_required(
    account: Account,
    *,
    can_auto_refresh: bool,
    expiry: ClassifiedExpiry,
    provider_known: bool,
) -> bool:
    """Return whether the user needs to log in or fix config."""
    if not provider_known:
        return True
    if account.last_refresh_status is RefreshStatus.FAILED:
        return True
    if isinstance(expiry, InvalidExpiry):
        return True
    return isinstance(expiry, ExpiredExpiry) and not can_auto_refresh


def _expires_at_local(expiry: ClassifiedExpiry) -> str | None:
    """Render expiry as a local ISO timestamp."""
    expires_at = _expiry_time(expiry)
    if expires_at is None:
        return None
    return expires_at.astimezone().isoformat()


def _identity_fingerprint(account: Account) -> str | None:
    """Return a short provider identity fingerprint, never a token."""
    value = account.provider_account_id
    if not value:
        return None
    if len(value) <= _IDENTITY_FULL_MAX_LENGTH:
        return value
    return f"{value[:8]}…{value[-4:]}"


def _expiry_time(expiry: ClassifiedExpiry) -> datetime | None:
    """Return the authoritative time from a classified expiry."""
    if isinstance(expiry, ValidExpiry | ExpiredExpiry):
        return expiry.at
    return None


def _format_machine_time(value: datetime) -> str:
    """Encode one doctor JSON timestamp as canonical UTC text."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_machine_time(value: datetime | None) -> str | None:
    """Encode an optional doctor JSON timestamp."""
    return _format_machine_time(value) if value is not None else None


def _diagnostic_dict(diagnostic: AccountDiagnostic) -> dict[str, object]:
    """Build one secret-free JSON-ready doctor record."""
    resets = diagnostic.heartbeat_window_resets
    return {
        "label": diagnostic.label,
        "provider": diagnostic.provider.value,
        "plan": diagnostic.plan,
        "usage_route": diagnostic.usage_route,
        "has_refresh_token": diagnostic.has_refresh_token,
        "expires_at": _optional_machine_time(diagnostic.expires_at),
        "expires_at_local": diagnostic.expires_at_local,
        "identity_fingerprint": diagnostic.identity_fingerprint,
        "can_auto_refresh": diagnostic.can_auto_refresh,
        "expiry_state": diagnostic.expiry_state.value,
        "last_refresh_at": _optional_machine_time(diagnostic.last_refresh_at),
        "last_refresh_status": (
            diagnostic.last_refresh_status.value
            if diagnostic.last_refresh_status is not None
            else None
        ),
        "last_refresh_error": diagnostic.last_refresh_error,
        "heartbeat_supported": diagnostic.heartbeat_supported,
        "heartbeat_enabled": diagnostic.heartbeat_enabled,
        "heartbeat": diagnostic.heartbeat,
        "heartbeat_5h_reset_at": _optional_machine_time(
            diagnostic.heartbeat_5h_reset_at
        ),
        "heartbeat_window_resets": (
            {
                target_id: _format_machine_time(reset_at)
                for target_id, reset_at in resets.items()
            }
            if resets is not None
            else None
        ),
        "heartbeat_targets": (
            list(diagnostic.heartbeat_targets)
            if diagnostic.heartbeat_targets is not None
            else None
        ),
        "last_heartbeat_at": _optional_machine_time(
            diagnostic.last_heartbeat_at
        ),
        "last_heartbeat_status": (
            diagnostic.last_heartbeat_status.value
            if diagnostic.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error": diagnostic.last_heartbeat_error,
        "manual_action_required": diagnostic.manual_action_required,
    }
