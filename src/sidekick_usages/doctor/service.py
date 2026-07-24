"""Read-only account diagnostic service."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ClassifiedExpiry,
    ExpiredExpiry,
    InvalidExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ExpiryState,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.claude.lifetime import (
    ClaudeLoginRenewalState,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.credentials import (
    DoctorCredentialKind,
    IdentityState,
    access_expiry_display,
    authentication_label,
    diagnose_credentials,
    expiry_time,
    refresh_expiry_display,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import heartbeat_supported_label
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.serialization.json import JsonObject, JsonValue

type DoctorResult = DoctorReadyResult | DoctorFailedResult


@dataclass(frozen=True)
class AccountDiagnostic:
    """Public doctor data for one account."""

    label: AccountLabel
    provider: ProviderId
    provider_available: bool
    plan: str
    usage_route: str
    has_refresh_token: bool
    credential_kind: DoctorCredentialKind
    access_expires_at: datetime | None
    access_expiry_state: ExpiryState
    access_expiry_display: str
    refresh_expires_at: datetime | None
    refresh_expiry_state: ExpiryState
    refresh_expiry_display: str
    login_renewal_state: ClaudeLoginRenewalState
    identity_state: IdentityState
    can_auto_refresh: bool
    last_refresh_at: datetime | None
    last_refresh_status: RefreshStatus | None
    last_refresh_error: str | None
    heartbeat_supported: bool
    heartbeat_enabled: bool
    heartbeat: str
    heartbeat_window_resets: Mapping[str, datetime] | None
    heartbeat_targets: tuple[str, ...] | None
    last_heartbeat_at: datetime | None
    last_heartbeat_status: HeartbeatStatus | None
    last_heartbeat_error: str | None
    manual_action_required: bool


@dataclass(frozen=True, slots=True)
class DoctorReadyResult:
    """Completed diagnostics for the current persistence store."""

    diagnostics: tuple[AccountDiagnostic, ...]
    persistence: PersistenceStatus
    refresh_state: CredentialRefreshState
    supervisor: SupervisorHealth


@dataclass(frozen=True, slots=True)
class DoctorFailedResult:
    """Completed bounded failure from doctor composition."""

    failure: PersistenceFailure
    supervisor: SupervisorHealth


class DoctorService:
    """Build read-only app health diagnostics."""

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
        credentials = diagnose_credentials(
            account,
            reference_time=reference_time,
            provider_registered=provider is not None,
        )
        manual_action_required = _manual_action_required(
            account,
            can_auto_refresh=credentials.can_auto_refresh,
            access_expiry=credentials.access_expiry,
            refresh_expiry=credentials.refresh_expiry,
            login_renewal_state=credentials.login_renewal_state,
            provider_known=provider is not None,
        )
        return AccountDiagnostic(
            label=account.label,
            provider=account.provider_id,
            provider_available=provider is not None,
            plan=account.plan,
            usage_route=usage_route(account),
            has_refresh_token=bool(account.refresh_token),
            credential_kind=credentials.kind,
            access_expires_at=expiry_time(credentials.access_expiry),
            access_expiry_state=credentials.access_expiry.state,
            access_expiry_display=access_expiry_display(
                credentials.kind,
                credentials.access_expiry,
                reference_time,
            ),
            refresh_expires_at=expiry_time(credentials.refresh_expiry),
            refresh_expiry_state=credentials.refresh_expiry.state,
            refresh_expiry_display=refresh_expiry_display(
                credentials.refresh_expiry
            ),
            login_renewal_state=credentials.login_renewal_state,
            identity_state=credentials.identity_state,
            can_auto_refresh=credentials.can_auto_refresh,
            last_refresh_at=account.last_refresh_at,
            last_refresh_status=account.last_refresh_status,
            last_refresh_error=account.last_refresh_error,
            heartbeat_supported=bool(
                heartbeat_provider and heartbeat_provider.supports(account)
            ),
            heartbeat_enabled=account.heartbeat_enabled,
            heartbeat=heartbeat_supported_label(account, heartbeat_provider),
            heartbeat_window_resets=account.heartbeat_window_resets,
            heartbeat_targets=account.heartbeat_targets,
            last_heartbeat_at=account.last_heartbeat_at,
            last_heartbeat_status=account.last_heartbeat_status,
            last_heartbeat_error=account.last_heartbeat_error,
            manual_action_required=manual_action_required,
        )


def usage_route(account: Account) -> str:
    """Return the provider route sidekick-usages will use for usage."""
    match account.credentials:
        case ClaudeSetupTokenCredentials():
            return "/v1/messages headers"
        case ClaudeLoginCredentials():
            return "/api/oauth/usage"
        case CodexCredentials():
            return "/backend-api/codex/usage"
        case unexpected:
            assert_never(unexpected)


def render_doctor(
    result: DoctorResult,
    *,
    width: int,
) -> RenderableType:
    """Build the human doctor view without printing."""
    parts: list[RenderableType] = [
        brand_header(width, section="doctor · account diagnostics")
    ]
    if isinstance(result, DoctorReadyResult):
        parts.extend(_supervisor_lines(result.supervisor))
        parts.extend(_persistence_lines(result.persistence))
        parts.append(
            Text("  credential refresh: " + result.refresh_state.kind.value)
        )
        diagnostics = result.diagnostics
    elif isinstance(result, DoctorFailedResult):
        parts.extend(_supervisor_lines(result.supervisor))
        parts.extend(_persistence_failure_lines(result.failure))
        diagnostics = ()
    else:
        assert_never(result)
    if diagnostics:
        parts.append(Text(""))
    for index, diagnostic in enumerate(diagnostics):
        if index:
            parts.append(Text(""))
        suffix = (
            f" · {diagnostic.plan}" if diagnostic.plan != "unknown" else ""
        )
        parts.append(
            Text.from_markup(
                f"{diagnostic.label}  [{diagnostic.provider}{suffix}]"
            )
        )
        parts.extend(_auth_lines(diagnostic))
        parts.extend(_heartbeat_lines(diagnostic))
        parts.append(
            Text(
                "  manual action: "
                + ("yes" if diagnostic.manual_action_required else "no")
            )
        )
    return Group(*parts)


def doctor_json(result: DoctorResult) -> JsonObject:
    """Build recursively typed doctor JSON from one completed result."""
    diagnostics: tuple[AccountDiagnostic, ...]
    persistence: JsonObject
    if isinstance(result, DoctorReadyResult):
        diagnostics = result.diagnostics
        persistence = _persistence_dict(result.persistence)
        persistence["credential_refresh"] = result.refresh_state.kind.value
    elif isinstance(result, DoctorFailedResult):
        diagnostics = ()
        persistence = _persistence_failure_dict(result.failure)
    else:
        assert_never(result)
    accounts: list[JsonValue] = [
        _diagnostic_dict(diagnostic) for diagnostic in diagnostics
    ]
    return {
        "accounts": accounts,
        "persistence": persistence,
        "supervisor": _supervisor_dict(result.supervisor),
    }


def _supervisor_lines(health: SupervisorHealth) -> tuple[Text, ...]:
    """Build independent human-readable supervisor health."""
    supervisor_version = (
        "unavailable"
        if health.supervisor_version is None
        else str(health.supervisor_version)
    )
    return (
        Text("supervisor"),
        Text(f"  backend: {health.backend}"),
        Text(f"  CLI version: {health.cli_version}"),
        Text(f"  supervisor version: {supervisor_version}"),
        Text(f"  platform: {health.platform}"),
        Text(f"  process: {health.process}"),
        Text(f"  protocol: {health.protocol}"),
        Text(f"  queue: {health.queue}"),
        Text(f"  journal: {health.journal}"),
        Text(f"  broker: {health.broker}"),
    )


def _supervisor_dict(health: SupervisorHealth) -> JsonObject:
    """Build machine-readable independent supervisor health."""
    return {
        "backend": health.backend.value,
        "cli_version": str(health.cli_version),
        "supervisor_version": (
            None
            if health.supervisor_version is None
            else str(health.supervisor_version)
        ),
        "platform": health.platform.value,
        "process": health.process.value,
        "protocol": health.protocol.value,
        "queue": health.queue.value,
        "journal": health.journal.value,
        "broker": health.broker.value,
    }


def _persistence_lines(status: PersistenceStatus) -> tuple[Text, ...]:
    """Build human-readable current persistence status."""
    return (
        Text("persistence"),
        Text(f"  state: {status.state}"),
        Text(f"  path: {status.path}"),
        Text(f"  validated accounts: {status.account_count}"),
    )


def _persistence_dict(status: PersistenceStatus) -> JsonObject:
    """Build machine-readable current persistence status."""
    return {
        "state": status.state.value,
        "path": str(status.path),
        "account_count": status.account_count,
    }


def _persistence_failure_lines(
    failure: PersistenceFailure,
) -> tuple[Text, ...]:
    """Build safe human lines for one passive composition failure."""
    lines = [
        Text("persistence"),
        Text(f"  state: {failure.code}"),
        Text(f"  path: {failure.path}"),
        Text(f"  message: {failure.message}"),
    ]
    if failure.artifact_basename is not None:
        lines.append(Text(f"  artifact: {failure.artifact_basename}"))
    return tuple(lines)


def _persistence_failure_dict(
    failure: PersistenceFailure,
) -> JsonObject:
    """Build one secret-free machine-readable composition failure."""
    return {
        "state": failure.code.value,
        "account_count": None,
        "path": str(failure.path),
        "artifact_basename": failure.artifact_basename,
        "message": failure.message,
    }


def _auth_lines(diagnostic: AccountDiagnostic) -> tuple[Text, ...]:
    """Build auth and refresh lines for one account."""
    lines = [
        Text(
            "  authentication: "
            + authentication_label(diagnostic.credential_kind)
        ),
        Text(
            "  provider adapter: "
            + ("available" if diagnostic.provider_available else "unavailable")
        ),
        Text(f"  usage route: {diagnostic.usage_route}"),
        Text(
            "  refresh token: "
            + ("present" if diagnostic.has_refresh_token else "none")
        ),
        Text(
            "  auto-refresh: "
            + ("yes" if diagnostic.can_auto_refresh else "no")
        ),
    ]
    if diagnostic.credential_kind is DoctorCredentialKind.SETUP_TOKEN:
        lines.append(
            Text(
                "  setup-token expiry: provider does not expose the "
                "token's issued-at timestamp"
            )
        )
    else:
        lines.append(
            Text("  access token expires: " + diagnostic.access_expiry_display)
        )
    if diagnostic.credential_kind is DoctorCredentialKind.SUBSCRIPTION_LOGIN:
        lines.append(
            Text("  login expires: " + diagnostic.refresh_expiry_display)
        )
        if (
            diagnostic.login_renewal_state
            is ClaudeLoginRenewalState.RENEWAL_DUE
        ):
            lines.append(Text("  login renewal: required within five days"))
        elif diagnostic.login_renewal_state in (
            ClaudeLoginRenewalState.EXPIRED,
            ClaudeLoginRenewalState.INVALID,
        ):
            lines.append(Text("  login renewal: required now"))
    if diagnostic.last_refresh_status:
        lines.append(Text(f"  last refresh: {diagnostic.last_refresh_status}"))
    if diagnostic.last_refresh_error:
        lines.append(Text(f"  error: {diagnostic.last_refresh_error}"))
    return tuple(lines)


def _heartbeat_lines(diagnostic: AccountDiagnostic) -> tuple[Text, ...]:
    """Build heartbeat lines for one account."""
    lines = [
        Text(
            "  heartbeat supported: "
            + ("yes" if diagnostic.heartbeat_supported else "no")
        ),
        Text(f"  heartbeat: {diagnostic.heartbeat}"),
        Text(
            "  heartbeat enabled: "
            + ("yes" if diagnostic.heartbeat_enabled else "no")
        ),
    ]
    if diagnostic.heartbeat_window_resets:
        lines.extend(
            Text(
                f"  cached {target_id} reset: {_format_machine_time(reset_at)}"
            )
            for target_id, reset_at in (
                diagnostic.heartbeat_window_resets.items()
            )
        )
    if diagnostic.heartbeat_targets:
        lines.append(
            Text(
                "  heartbeat targets: "
                + ", ".join(diagnostic.heartbeat_targets)
            )
        )
    if diagnostic.last_heartbeat_status:
        lines.append(
            Text(f"  last heartbeat: {diagnostic.last_heartbeat_status}")
        )
    if diagnostic.last_heartbeat_error:
        lines.append(
            Text(f"  heartbeat error: {diagnostic.last_heartbeat_error}")
        )
    return tuple(lines)


def doctor_exit_code(diagnostics: Sequence[AccountDiagnostic]) -> ExitCode:
    """Return 1 when doctor found an account needing manual action."""
    if any(d.manual_action_required for d in diagnostics):
        return ExitCode.MANUAL_ACTION
    return ExitCode.SUCCESS


def _manual_action_required(
    account: Account,
    *,
    can_auto_refresh: bool,
    access_expiry: ClassifiedExpiry,
    refresh_expiry: ClassifiedExpiry,
    login_renewal_state: ClaudeLoginRenewalState,
    provider_known: bool,
) -> bool:
    """Return whether the user needs to log in or fix config."""
    if not provider_known:
        return True
    if account.last_refresh_status is RefreshStatus.FAILED:
        return True
    if isinstance(access_expiry, InvalidExpiry):
        return True
    if isinstance(account.credentials, ClaudeLoginCredentials) and isinstance(
        refresh_expiry,
        ExpiredExpiry | InvalidExpiry,
    ):
        return True
    if login_renewal_state is ClaudeLoginRenewalState.RENEWAL_DUE:
        return True
    return isinstance(access_expiry, ExpiredExpiry) and not can_auto_refresh


def _format_machine_time(value: datetime) -> str:
    """Encode one doctor JSON timestamp as canonical UTC text."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_machine_time(value: datetime | None) -> str | None:
    """Encode an optional doctor JSON timestamp."""
    return _format_machine_time(value) if value is not None else None


def _diagnostic_dict(diagnostic: AccountDiagnostic) -> JsonObject:
    """Build one secret-free JSON-ready doctor record."""
    resets = diagnostic.heartbeat_window_resets
    window_resets: JsonValue = None
    if resets is not None:
        encoded_resets: JsonObject = {}
        for target_id, reset_at in resets.items():
            encoded_resets[target_id] = _format_machine_time(reset_at)
        window_resets = encoded_resets
    targets: JsonValue = None
    if diagnostic.heartbeat_targets is not None:
        encoded_targets: list[JsonValue] = []
        encoded_targets.extend(diagnostic.heartbeat_targets)
        targets = encoded_targets
    return {
        "label": str(diagnostic.label),
        "provider": diagnostic.provider.value,
        "provider_available": diagnostic.provider_available,
        "plan": diagnostic.plan,
        "usage_route": diagnostic.usage_route,
        "has_refresh_token": diagnostic.has_refresh_token,
        "credential_kind": diagnostic.credential_kind.value,
        "access_expires_at": _optional_machine_time(
            diagnostic.access_expires_at
        ),
        "access_expiry_state": diagnostic.access_expiry_state.value,
        "refresh_expires_at": _optional_machine_time(
            diagnostic.refresh_expires_at
        ),
        "refresh_expiry_state": diagnostic.refresh_expiry_state.value,
        "login_renewal_state": diagnostic.login_renewal_state.value,
        "identity_state": diagnostic.identity_state.value,
        "can_auto_refresh": diagnostic.can_auto_refresh,
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
        "heartbeat_window_resets": window_resets,
        "heartbeat_targets": targets,
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
