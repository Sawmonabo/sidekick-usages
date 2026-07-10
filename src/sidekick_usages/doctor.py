"""Read-only account diagnostics for ``sidekick-usages doctor``."""

import shlex
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
from sidekick_usages.persistence.migrations.location import (
    BlockedLocationSelection,
    LocationCandidate,
    LocationMigrationAssessment,
    ReadyLocationSelection,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthAccountAssessment,
    PrivateAuthMigrationAssessment,
    PrivateAuthMigrationFailure,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import PROFILE_SCOPE
from sidekick_usages.serialization import JsonObject, JsonValue

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


@dataclass(frozen=True, slots=True)
class DoctorReadyResult:
    """Completed diagnostics for one ready persistence location."""

    diagnostics: tuple[AccountDiagnostic, ...]
    assessment: LocationMigrationAssessment[ReadyLocationSelection]


@dataclass(frozen=True, slots=True)
class DoctorBlockedResult:
    """Completed location assessment that blocks account diagnostics."""

    assessment: LocationMigrationAssessment[BlockedLocationSelection]


@dataclass(frozen=True, slots=True)
class DoctorFailedResult:
    """Completed bounded failure from doctor composition."""

    failure: PersistenceCompositionFailure


type DoctorResult = (
    DoctorReadyResult | DoctorBlockedResult | DoctorFailedResult
)


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
    result: DoctorResult,
    *,
    width: int,
) -> RenderableType:
    """Build the human doctor view without printing."""
    parts: list[RenderableType] = [
        brand_header(width, section="doctor · account diagnostics")
    ]
    if isinstance(result, DoctorReadyResult):
        parts.extend(_location_lines(result.assessment))
        diagnostics = result.diagnostics
    elif isinstance(result, DoctorBlockedResult):
        parts.extend(_location_lines(result.assessment))
        diagnostics = ()
    elif isinstance(result, DoctorFailedResult):
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
        persistence = _location_dict(result.assessment)
    elif isinstance(result, DoctorBlockedResult):
        diagnostics = ()
        persistence = _location_dict(result.assessment)
    elif isinstance(result, DoctorFailedResult):
        diagnostics = ()
        persistence = _persistence_failure_dict(result.failure)
    else:
        assert_never(result)
    accounts: list[JsonValue] = [
        _diagnostic_dict(diagnostic) for diagnostic in diagnostics
    ]
    return {"accounts": accounts, "persistence": persistence}


def _location_lines[S: ReadyLocationSelection | BlockedLocationSelection](
    assessment: LocationMigrationAssessment[S],
) -> tuple[Text, ...]:
    """Build safe human lines for one complete location assessment."""
    lines = [
        Text("persistence"),
        Text(f"  location: {assessment.selection.code}"),
        Text(f"  source: {assessment.source}"),
        Text(f"  destination: {assessment.destination}"),
        Text(
            "  write blocked: " + ("yes" if assessment.write_blocked else "no")
        ),
    ]
    if len(assessment.candidates) == 1:
        candidate = assessment.candidates[0]
        lines.append(Text(f"  candidate: {candidate.role}"))
        lines.extend(_schema_lines(candidate.assessment, indent="  "))
    elif assessment.candidates:
        lines.append(Text("  candidates:"))
        for candidate in assessment.candidates:
            lines.append(Text(f"    {candidate.role}:"))
            lines.extend(_schema_lines(candidate.assessment, indent="      "))
    lines.extend(_private_auth_lines(assessment.private_auth_summary))
    if assessment.artifact_basename is not None:
        lines.append(Text(f"  artifact: {assessment.artifact_basename}"))
    if assessment.next_command is not None:
        lines.append(Text("  next: " + " ".join(assessment.next_command)))
    if assessment.issues:
        lines.append(Text("  findings:"))
        for issue in assessment.issues:
            artifact = (
                f" ({issue.artifact_basename})"
                if issue.artifact_basename is not None
                else ""
            )
            lines.append(Text(f"    {issue.code}{artifact}: {issue.message}"))
    return tuple(lines)


def _schema_lines(
    assessment: PersistenceAssessment,
    *,
    indent: str,
) -> tuple[Text, ...]:
    count = (
        str(assessment.account_count)
        if assessment.account_count is not None
        else "unknown"
    )
    lines = [
        Text(f"{indent}state: {assessment.code}"),
        Text(f"{indent}generation: {assessment.generation}"),
        Text(f"{indent}path: {assessment.safe_path}"),
        Text(f"{indent}validated accounts: {count}"),
        Text(f"{indent}message: {assessment.message}"),
    ]
    if assessment.artifact_basename is not None:
        lines.append(Text(f"{indent}artifact: {assessment.artifact_basename}"))
    if assessment.next_command is not None:
        lines.append(
            Text(f"{indent}next: " + " ".join(assessment.next_command))
        )
    if assessment.guidance is not None:
        lines.append(Text(f"{indent}guidance: {assessment.guidance}"))
    return tuple(lines)


def _private_auth_lines(
    summary: PrivateAuthMigrationAssessment | PrivateAuthMigrationFailure,
) -> tuple[Text, ...]:
    if isinstance(summary, PrivateAuthMigrationFailure):
        lines = [
            Text(f"  private auth: {summary.code}"),
            Text(f"  private auth message: {summary.message}"),
        ]
        if summary.accounts:
            lines.append(
                Text(
                    "  private auth accounts: "
                    + ", ".join(str(label) for label in summary.accounts)
                )
            )
        return tuple(lines)
    lines = [Text(f"  private auth copies: {summary.copies_required}")]
    lines.extend(
        _private_auth_account_line(account) for account in summary.accounts
    )
    return tuple(lines)


def _private_auth_account_line(
    account: PrivateAuthAccountAssessment,
) -> Text:
    copy = " · copy required" if account.copy_required else ""
    return Text(f"  private auth {account.label}: {account.kind}{copy}")


def _location_dict[S: ReadyLocationSelection | BlockedLocationSelection](
    assessment: LocationMigrationAssessment[S],
) -> JsonObject:
    candidates: list[JsonValue] = [
        _candidate_dict(candidate) for candidate in assessment.candidates
    ]
    issues: list[JsonValue] = [
        {
            "code": issue.code.value,
            "artifact_basename": issue.artifact_basename,
            "message": issue.message,
        }
        for issue in assessment.issues
    ]
    return {
        "code": assessment.selection.code.value,
        "source": str(assessment.source),
        "destination": str(assessment.destination),
        "artifact_basename": assessment.artifact_basename,
        "write_blocked": assessment.write_blocked,
        "next_command": _command_json(assessment.next_command),
        "private_auth": _private_auth_dict(assessment.private_auth_summary),
        "candidates": candidates,
        "issues": issues,
    }


def _candidate_dict(candidate: LocationCandidate) -> JsonObject:
    return {
        "role": candidate.role.value,
        "path": str(candidate.path),
        "schema": _persistence_dict(candidate.assessment),
    }


def _persistence_dict(assessment: PersistenceAssessment) -> JsonObject:
    """Build one secret-free machine-readable schema record."""
    issues: list[JsonValue] = [
        {
            "code": issue.code.value,
            "artifact_basename": issue.artifact_basename,
            "message": issue.message,
        }
        for issue in assessment.issues
    ]
    return {
        "code": assessment.code.value,
        "generation": assessment.generation.value,
        "schema_version": assessment.schema_version,
        "account_count": assessment.account_count,
        "safe_path": str(assessment.safe_path),
        "artifact_basename": assessment.artifact_basename,
        "write_blocked": assessment.write_blocked,
        "next_command": _command_json(assessment.next_command),
        "message": assessment.message,
        "guidance": assessment.guidance,
        "issues": issues,
    }


def _private_auth_dict(
    summary: PrivateAuthMigrationAssessment | PrivateAuthMigrationFailure,
) -> JsonObject:
    if isinstance(summary, PrivateAuthMigrationFailure):
        accounts: list[JsonValue] = []
        accounts.extend(str(label) for label in summary.accounts)
        return {
            "code": summary.code.value,
            "message": summary.message,
            "accounts": accounts,
        }
    accounts: list[JsonValue] = [
        _private_auth_account_dict(account) for account in summary.accounts
    ]
    return {
        "copies_required": summary.copies_required,
        "accounts": accounts,
    }


def _private_auth_account_dict(
    account: PrivateAuthAccountAssessment,
) -> JsonObject:
    return {
        "label": str(account.label),
        "kind": account.kind.value,
        "copy_required": account.copy_required,
    }


def _persistence_failure_lines(
    failure: PersistenceCompositionFailure,
) -> tuple[Text, ...]:
    """Build safe human lines for one passive composition failure."""
    lines = [
        Text("persistence"),
        Text("  location: unavailable"),
        Text(f"  state: {failure.code}"),
        Text(f"  path: {failure.safe_path}"),
        Text(f"  message: {failure.message}"),
    ]
    if failure.artifact_basename is not None:
        lines.append(Text(f"  artifact: {failure.artifact_basename}"))
    if failure.guidance is not None:
        lines.append(Text(f"  guidance: {failure.guidance}"))
    if failure.next_command is not None:
        lines.append(Text("  next: " + shlex.join(failure.next_command)))
    return tuple(lines)


def _persistence_failure_dict(
    failure: PersistenceCompositionFailure,
) -> JsonObject:
    """Build one secret-free machine-readable composition failure."""
    issues: list[JsonValue] = [
        {
            "code": failure.code.value,
            "artifact_basename": failure.artifact_basename,
            "message": failure.message,
        }
    ]
    return {
        "code": failure.code.value,
        "generation": "unknown",
        "schema_version": None,
        "account_count": None,
        "safe_path": str(failure.safe_path),
        "artifact_basename": failure.artifact_basename,
        "write_blocked": True,
        "next_command": _command_json(failure.next_command),
        "message": failure.message,
        "guidance": failure.guidance,
        "issues": issues,
    }


def _command_json(command: tuple[str, ...] | None) -> JsonValue:
    if command is None:
        return None
    encoded: list[JsonValue] = []
    encoded.extend(command)
    return encoded


def _auth_lines(diagnostic: AccountDiagnostic) -> tuple[Text, ...]:
    """Build auth and refresh lines for one account."""
    lines = [
        Text(f"  usage route: {diagnostic.usage_route}"),
        Text(
            "  refresh token: "
            + ("present" if diagnostic.has_refresh_token else "none")
        ),
        Text(
            "  auto-refresh: "
            + ("yes" if diagnostic.can_auto_refresh else "no")
        ),
        Text(
            f"  expires: {diagnostic.expires_at_local}"
            if diagnostic.expires_at_local
            else "  expires: unknown"
        ),
    ]
    if diagnostic.identity_fingerprint:
        lines.append(Text(f"  identity: {diagnostic.identity_fingerprint}"))
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
    if diagnostic.heartbeat_5h_reset_at:
        lines.append(
            Text(
                "  cached 5h reset: "
                + _format_machine_time(diagnostic.heartbeat_5h_reset_at)
            )
        )
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
