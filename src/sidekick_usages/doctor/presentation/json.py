"""Machine-readable projection for Doctor diagnostics."""

from datetime import datetime
from typing import assert_never

from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
    ProviderCapabilityResult,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.accounts.models import (
    AccountDiagnostic,
    AuthorityDiagnostic,
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
)
from sidekick_usages.doctor.runtime.models import (
    ScheduledOperationDiagnostic,
    UnfinishedActivationDiagnostic,
)
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
from sidekick_usages.serialization.json import JsonObject, JsonValue


def doctor_json(result: DoctorResult) -> JsonObject:
    """Build recursively typed Doctor JSON from one completed result."""
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
        "provider_capabilities": _capability_dicts(result.capabilities),
        "service": _service_dict(result.supervisor),
        "operations": _operation_dict(
            result.supervisor,
            (
                result.scheduled_operations
                if isinstance(result, DoctorReadyResult)
                else ()
            ),
            (
                result.unfinished_activations
                if isinstance(result, DoctorReadyResult)
                else ()
            ),
        ),
        "persistence": persistence,
    }


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
        "wsl_rescue_configuration": health.rescue.value,
        "socket_ownership": health.socket.value,
        "peer_verification": health.peer.value,
        "protocol": health.protocol.value,
        "broker": health.broker.value,
    }


def _operation_dict(
    health: SupervisorHealth,
    scheduled: tuple[ScheduledOperationDiagnostic, ...],
    activations: tuple[UnfinishedActivationDiagnostic, ...],
) -> JsonObject:
    """Build machine-readable queue and journal health."""
    return {
        "queue": health.queue.value,
        "journal": health.journal.value,
        "scheduled": [
            _scheduled_operation_dict(operation)
            for operation in scheduled
        ],
        "unfinished_activations": [
            _unfinished_activation_dict(activation)
            for activation in activations
        ],
    }


def _capability_dicts(
    report: ProviderCapabilityReport,
) -> list[JsonValue]:
    """Build deterministic machine-readable capability evidence."""
    return [_capability_dict(result) for result in report.results]


def _capability_dict(result: ProviderCapabilityResult) -> JsonObject:
    """Build one provider capability result without provider output."""
    provenance = result.provenance
    executable: JsonObject | None = None
    if provenance is not None:
        executable = {
            "path": str(provenance.path),
            "version": result.version,
            "device": provenance.device,
            "inode": provenance.inode,
            "size": provenance.size,
            "modified_nanoseconds": provenance.modified_nanoseconds,
        }
    capabilities: JsonObject | None = None
    if isinstance(result.capabilities, ClaudeRuntimeCapabilities):
        capabilities = {"platform": result.capabilities.platform.value}
    elif isinstance(result.capabilities, CodexAppServerCapabilities):
        capabilities = {"schema_hash": result.capabilities.schema_hash}
    return {
        "provider": result.provider_id.value,
        "ready": result.ready,
        "failure_code": result.failure_code,
        "executable": executable,
        "capabilities": capabilities,
    }


def _scheduled_operation_dict(
    operation: ScheduledOperationDiagnostic,
) -> JsonObject:
    return {
        "provider": operation.provider_id.value,
        "account_label": (
            None
            if operation.account_label is None
            else str(operation.account_label)
        ),
        "kind": operation.kind.value,
        "state": operation.state.value,
        "due_at": canonical_timestamp(operation.due_at),
        "updated_at": canonical_timestamp(operation.updated_at),
        "attempts": operation.attempts,
        "failure_code": operation.failure_code,
    }


def _unfinished_activation_dict(
    activation: UnfinishedActivationDiagnostic,
) -> JsonObject:
    return {
        "provider": activation.provider_id.value,
        "target_label": str(activation.target_label),
        "phase": activation.phase.value,
        "started_at": canonical_timestamp(activation.started_at),
        "updated_at": canonical_timestamp(activation.updated_at),
        "failure_code": activation.failure_code,
    }


def _persistence_dict(status: PersistenceStatus) -> JsonObject:
    """Build machine-readable current persistence status."""
    return {
        "state": status.state.value,
        "path": str(status.path),
        "account_count": status.account_count,
    }


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


def _diagnostic_dict(diagnostic: AccountDiagnostic) -> JsonObject:
    """Build one secret-free JSON-ready logical account record."""
    resets = diagnostic.heartbeat_window_resets
    window_resets: JsonValue = None
    if resets is not None:
        encoded_resets: JsonObject = {}
        for target_id, reset_at in resets:
            encoded_resets[target_id] = canonical_timestamp(reset_at)
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
        "selected_generation_relation": (
            diagnostic.selected_generation_relation.value
        ),
        "metrics_freshness": diagnostic.metrics_freshness.value,
        "metrics_observed_at": _optional_machine_time(
            diagnostic.metrics_observed_at
        ),
        "warning": (
            diagnostic.warning.value
            if diagnostic.warning is not None
            else None
        ),
        "manual_action": (
            None
            if diagnostic.manual_action is None
            else list(diagnostic.manual_action)
        ),
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


def _optional_machine_time(value: datetime | None) -> str | None:
    """Encode an optional Doctor JSON timestamp."""
    return canonical_timestamp(value) if value is not None else None
