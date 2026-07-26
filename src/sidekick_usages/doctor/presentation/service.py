"""Human and JSON presentation for doctor diagnostics."""

from datetime import UTC, datetime
from typing import assert_never

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.core.accounts.types import CredentialAction
from sidekick_usages.credentials.claude.lifetime import (
    ClaudeLoginRenewalState,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.accounts.models import (
    AccountDiagnostic,
    AuthorityDiagnostic,
    DoctorAuthorityManagement,
    DoctorCredentialKind,
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
    IdentityState,
)
from sidekick_usages.doctor.runtime.types import DoctorAccountWarning
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue


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
        parts.extend(_service_lines(result.supervisor))
        parts.extend(_operation_lines(result.supervisor))
        parts.extend(_persistence_lines(result.persistence))
        parts.append(
            Text("  credential refresh: " + result.refresh_state.kind.value)
        )
        diagnostics = result.diagnostics
    elif isinstance(result, DoctorFailedResult):
        parts.extend(_service_lines(result.supervisor))
        parts.extend(_operation_lines(result.supervisor))
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
            Text(f"{diagnostic.label}  [{diagnostic.provider.value}{suffix}]")
        )
        parts.extend(_auth_lines(diagnostic))
        parts.extend(_runtime_lines(diagnostic))
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
        "service": _service_dict(result.supervisor),
        "operations": _operation_dict(result.supervisor),
        "persistence": persistence,
    }


def _service_lines(health: SupervisorHealth) -> tuple[Text, ...]:
    """Build independent resident-service health."""
    supervisor_version = (
        "unavailable"
        if health.supervisor_version is None
        else str(health.supervisor_version)
    )
    return (
        Text("service"),
        Text(f"  backend: {health.backend}"),
        Text(f"  CLI version: {health.cli_version}"),
        Text(f"  supervisor version: {supervisor_version}"),
        Text(f"  platform: {health.platform}"),
        Text(f"  process: {health.process}"),
        Text(f"  protocol: {health.protocol}"),
        Text(f"  broker: {health.broker}"),
    )


def _operation_lines(health: SupervisorHealth) -> tuple[Text, ...]:
    """Build independent durable queue and journal health."""
    return (
        Text("operations"),
        Text(f"  queue: {health.queue}"),
        Text(f"  journal: {health.journal}"),
    )


def _service_dict(health: SupervisorHealth) -> JsonObject:
    """Build machine-readable resident-service health."""
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
        "broker": health.broker.value,
    }


def _operation_dict(health: SupervisorHealth) -> JsonObject:
    """Build machine-readable queue and journal health."""
    return {
        "queue": health.queue.value,
        "journal": health.journal.value,
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
    """Build independent authority lines for one logical account."""
    lines = [
        Text(
            "  provider adapter: "
            + ("available" if diagnostic.provider_available else "unavailable")
        ),
        Text(f"  credential health: {diagnostic.credential_health}"),
        Text(f"  identity association: {diagnostic.identity_state}"),
    ]
    if diagnostic.identity_state is IdentityState.ASSOCIATION_REQUIRED:
        lines.append(
            Text(
                "  identity action: confirm the first subscription "
                "identity association"
            )
        )
    if diagnostic.setup_token is not None:
        lines.extend(_authority_lines(diagnostic.setup_token))
    if diagnostic.subscription is not None:
        lines.extend(_authority_lines(diagnostic.subscription))
    if diagnostic.last_refresh_status is not None:
        lines.append(Text(f"  last refresh: {diagnostic.last_refresh_status}"))
    if diagnostic.last_refresh_error is not None:
        lines.append(Text(f"  refresh error: {diagnostic.last_refresh_error}"))
    return tuple(lines)


def _authority_lines(
    diagnostic: AuthorityDiagnostic,
) -> tuple[Text, ...]:
    """Build secret-free lines for one credential authority."""
    lines = [
        Text("  authentication: " + _authentication_label(diagnostic.kind)),
        Text("    management: " + _management_label(diagnostic.management)),
        Text(f"    health: {diagnostic.health}"),
        Text(f"    usage route: {diagnostic.usage_route}"),
        Text(
            "    auto-refresh: "
            + ("yes" if diagnostic.can_auto_refresh else "no")
        ),
    ]
    if diagnostic.kind is DoctorCredentialKind.SETUP_TOKEN:
        if diagnostic.access_expires_at is None:
            lines.append(
                Text(
                    "    setup-token expiry: provider does not expose "
                    "the token's issued-at timestamp"
                )
            )
        else:
            lines.append(
                Text(
                    "    setup token expires: "
                    + diagnostic.access_expiry_display
                )
            )
    else:
        lines.append(
            Text(
                "    access token expires: " + diagnostic.access_expiry_display
            )
        )
    if diagnostic.kind is DoctorCredentialKind.SUBSCRIPTION_LOGIN:
        lines.append(
            Text("    login expires: " + diagnostic.refresh_expiry_display)
        )
        lines.extend(_renewal_lines(diagnostic.login_renewal_state))
    if (
        diagnostic.provider_action is not None
        and diagnostic.provider_action is not CredentialAction.NONE
    ):
        lines.append(
            Text(f"    provider action: {diagnostic.provider_action}")
        )
    return tuple(lines)


def _renewal_lines(
    state: ClaudeLoginRenewalState,
) -> tuple[Text, ...]:
    """Build Claude login-renewal action copy."""
    if state is ClaudeLoginRenewalState.RENEWAL_DUE:
        return (Text("    login renewal: required within five days"),)
    if state in {
        ClaudeLoginRenewalState.EXPIRED,
        ClaudeLoginRenewalState.INVALID,
    }:
        return (Text("    login renewal: required now"),)
    return ()


def _heartbeat_lines(diagnostic: AccountDiagnostic) -> tuple[Text, ...]:
    """Build heartbeat lines for one account."""
    lines = [
        Text(f"  heartbeat supported: {diagnostic.heartbeat_support}"),
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
            for target_id, reset_at in diagnostic.heartbeat_window_resets
        )
    if diagnostic.heartbeat_targets:
        lines.append(
            Text(
                "  heartbeat targets: "
                + ", ".join(diagnostic.heartbeat_targets)
            )
        )
    if diagnostic.last_heartbeat_status is not None:
        lines.append(
            Text(f"  last heartbeat: {diagnostic.last_heartbeat_status}")
        )
    if diagnostic.last_heartbeat_error is not None:
        lines.append(
            Text(f"  heartbeat error: {diagnostic.last_heartbeat_error}")
        )
    return tuple(lines)


def _runtime_lines(diagnostic: AccountDiagnostic) -> tuple[Text, ...]:
    """Build native relation, cached metrics, and account warning lines."""
    lines = [
        Text(f"  native relation: {diagnostic.native_relation}"),
    ]
    if diagnostic.metrics_observed_at is None:
        lines.append(Text("  metrics: unavailable"))
    else:
        lines.append(
            Text(
                f"  metrics: {diagnostic.metrics_freshness} · "
                f"{_format_machine_time(diagnostic.metrics_observed_at)}"
            )
        )
    if diagnostic.warning is DoctorAccountWarning.LOGIN_REQUIRED:
        lines.append(
            Text(
                "  warning: official provider login is required for this "
                "account"
            )
        )
    elif diagnostic.warning is DoctorAccountWarning.RECONCILIATION_REQUIRED:
        lines.append(
            Text(
                "  warning: this account's native provider relation "
                "requires reconciliation"
            )
        )
    return tuple(lines)


def _authentication_label(kind: DoctorCredentialKind) -> str:
    """Return the product label for one credential authority."""
    if kind is DoctorCredentialKind.SETUP_TOKEN:
        return "setup token"
    if kind is DoctorCredentialKind.SUBSCRIPTION_LOGIN:
        return "subscription login"
    return "Codex login"


def _management_label(management: DoctorAuthorityManagement) -> str:
    """Return concise credential-state ownership copy."""
    if management is DoctorAuthorityManagement.PROVIDER_MANAGED:
        return "provider managed"
    return "Sidekick stored"


def _format_machine_time(value: datetime) -> str:
    """Encode one doctor JSON timestamp as canonical UTC text."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_machine_time(value: datetime | None) -> str | None:
    """Encode an optional doctor JSON timestamp."""
    return _format_machine_time(value) if value is not None else None


def _diagnostic_dict(diagnostic: AccountDiagnostic) -> JsonObject:
    """Build one secret-free JSON-ready logical account record."""
    resets = diagnostic.heartbeat_window_resets
    window_resets: JsonValue = None
    if resets is not None:
        encoded_resets: JsonObject = {}
        for target_id, reset_at in resets:
            encoded_resets[target_id] = _format_machine_time(reset_at)
        window_resets = encoded_resets
    targets: JsonValue = None
    if diagnostic.heartbeat_targets is not None:
        encoded_targets: list[JsonValue] = []
        encoded_targets.extend(diagnostic.heartbeat_targets)
        targets = encoded_targets
    authorities: JsonObject = {
        "setup_token": _optional_authority_dict(diagnostic.setup_token),
        "subscription": _optional_authority_dict(diagnostic.subscription),
    }
    return {
        "label": str(diagnostic.label),
        "provider": diagnostic.provider.value,
        "provider_available": diagnostic.provider_available,
        "plan": diagnostic.plan,
        "credential_health": diagnostic.credential_health.value,
        "identity_state": diagnostic.identity_state.value,
        "authorities": authorities,
        "last_refresh_at": _optional_machine_time(diagnostic.last_refresh_at),
        "last_refresh_status": (
            diagnostic.last_refresh_status.value
            if diagnostic.last_refresh_status is not None
            else None
        ),
        "last_refresh_error": diagnostic.last_refresh_error,
        "heartbeat_support": diagnostic.heartbeat_support.value,
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
        "native_relation": diagnostic.native_relation.value,
        "metrics_freshness": diagnostic.metrics_freshness.value,
        "metrics_observed_at": _optional_machine_time(
            diagnostic.metrics_observed_at
        ),
        "warning": (
            diagnostic.warning.value
            if diagnostic.warning is not None
            else None
        ),
        "manual_action_required": diagnostic.manual_action_required,
    }


def _optional_authority_dict(
    diagnostic: AuthorityDiagnostic | None,
) -> JsonObject | None:
    """Build one optional authority object."""
    if diagnostic is None:
        return None
    return {
        "kind": diagnostic.kind.value,
        "management": diagnostic.management.value,
        "health": diagnostic.health.value,
        "usage_route": diagnostic.usage_route,
        "access_expires_at": _optional_machine_time(
            diagnostic.access_expires_at
        ),
        "access_expiry_state": diagnostic.access_expiry_state.value,
        "refresh_expires_at": _optional_machine_time(
            diagnostic.refresh_expires_at
        ),
        "refresh_expiry_state": diagnostic.refresh_expiry_state.value,
        "login_renewal_state": diagnostic.login_renewal_state.value,
        "provider_action": (
            diagnostic.provider_action.value
            if diagnostic.provider_action is not None
            else None
        ),
        "can_auto_refresh": diagnostic.can_auto_refresh,
        "manual_action_required": diagnostic.manual_action_required,
    }
