"""Human and JSON presentation for doctor diagnostics."""

import shlex
from typing import assert_never

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.core.accounts.types import CredentialAction
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
)
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
from sidekick_usages.doctor.runtime.models import (
    ScheduledOperationDiagnostic,
    UnfinishedActivationDiagnostic,
)
from sidekick_usages.doctor.runtime.types import DoctorAccountWarning
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.persistence.time_codec import canonical_timestamp
from sidekick_usages.providers.claude.managed.models import (
    ClaudeRuntimeCapabilities,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)


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
        parts.extend(_capability_lines(result.capabilities))
        parts.extend(
            _operation_lines(
                result.supervisor,
                result.scheduled_operations,
                result.unfinished_activations,
            )
        )
        parts.extend(_persistence_lines(result.persistence))
        parts.append(
            Text("  credential refresh: " + result.refresh_state.kind.value)
        )
        diagnostics = result.diagnostics
    elif isinstance(result, DoctorFailedResult):
        parts.extend(_service_lines(result.supervisor))
        parts.extend(_capability_lines(result.capabilities))
        parts.extend(_operation_lines(result.supervisor, (), ()))
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
        action = (
            "none"
            if diagnostic.manual_action is None
            else shlex.join(diagnostic.manual_action)
        )
        parts.append(Text("  manual action: " + action))
    return Group(*parts)


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
        Text(f"  WSL rescue configuration: {health.rescue}"),
        Text(f"  socket ownership: {health.socket}"),
        Text(f"  peer verification: {health.peer}"),
        Text(f"  protocol: {health.protocol}"),
        Text(f"  broker: {health.broker}"),
    )


def _operation_lines(
    health: SupervisorHealth,
    scheduled: tuple[ScheduledOperationDiagnostic, ...],
    activations: tuple[UnfinishedActivationDiagnostic, ...],
) -> tuple[Text, ...]:
    """Build independent durable queue and journal health."""
    lines = [
        Text("operations"),
        Text(f"  queue: {health.queue}"),
        Text(f"  journal: {health.journal}"),
    ]
    if not scheduled:
        lines.append(Text("  due and retry: none"))
    else:
        lines.extend(_scheduled_operation_lines(scheduled))
    if not activations:
        lines.append(Text("  unfinished activation: none"))
    else:
        lines.extend(_unfinished_activation_lines(activations))
    return tuple(lines)


def _capability_lines(
    report: ProviderCapabilityReport,
) -> tuple[Text, ...]:
    """Build exact executable and provider-capability evidence."""
    lines = [Text("provider capabilities")]
    for result in report.results:
        state = "ready" if result.ready else "unavailable"
        lines.append(Text(f"  {result.provider_id}: {state}"))
        provenance = result.provenance
        if provenance is not None:
            lines.extend(
                (
                    Text(f"    executable: {provenance.path}"),
                    Text(f"    version: {result.version}"),
                    Text(
                        "    file identity: "
                        f"{provenance.device}:{provenance.inode} · "
                        f"{provenance.size} bytes · "
                        f"mtime {provenance.modified_nanoseconds}ns"
                    ),
                )
            )
        if result.failure_code is not None:
            lines.append(Text(f"    failure: {result.failure_code}"))
        capabilities = result.capabilities
        if isinstance(capabilities, ClaudeRuntimeCapabilities):
            lines.append(Text(f"    platform: {capabilities.platform}"))
        elif isinstance(capabilities, CodexAppServerCapabilities):
            lines.append(Text(f"    schema: {capabilities.schema_hash}"))
    return tuple(lines)


def _scheduled_operation_lines(
    scheduled: tuple[ScheduledOperationDiagnostic, ...],
) -> tuple[Text, ...]:
    lines: list[Text] = []
    for operation in scheduled:
        owner = (
            "provider"
            if operation.account_label is None
            else str(operation.account_label)
        )
        lines.append(
            Text(
                f"  {operation.provider_id}/{owner} "
                f"{operation.kind}: {operation.state} · "
                f"due {canonical_timestamp(operation.due_at)} · "
                f"attempts {operation.attempts}"
            )
        )
        if operation.failure_code is not None:
            lines.append(Text(f"    failure: {operation.failure_code}"))
    return tuple(lines)


def _unfinished_activation_lines(
    activations: tuple[UnfinishedActivationDiagnostic, ...],
) -> tuple[Text, ...]:
    lines: list[Text] = []
    for activation in activations:
        lines.append(
            Text(
                f"  unfinished activation: {activation.provider_id}/"
                f"{activation.target_label} · {activation.phase} · "
                f"updated {canonical_timestamp(activation.updated_at)}"
            )
        )
        if activation.failure_code is not None:
            lines.append(Text(f"    failure: {activation.failure_code}"))
    return tuple(lines)


def _persistence_lines(status: PersistenceStatus) -> tuple[Text, ...]:
    """Build human-readable current persistence status."""
    return (
        Text("persistence"),
        Text(f"  state: {status.state}"),
        Text(f"  path: {status.path}"),
        Text(f"  validated accounts: {status.account_count}"),
    )


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
                f"  cached {target_id} reset: {canonical_timestamp(reset_at)}"
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
        Text(
            f"  selected generation: {diagnostic.selected_generation_relation}"
        ),
    ]
    if diagnostic.metrics_observed_at is None:
        lines.append(Text("  metrics: unavailable"))
    else:
        lines.append(
            Text(
                f"  metrics: {diagnostic.metrics_freshness} · "
                f"{canonical_timestamp(diagnostic.metrics_observed_at)}"
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
