"""Machine-readable projection for Doctor diagnostics."""

from datetime import datetime
from typing import assert_never

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
    ProviderCapabilityResult,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION
from sidekick_usages.doctor.accounts.models import (
    AccountDiagnostic,
    AuthorityDiagnostic,
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
)
from sidekick_usages.doctor.runtime.models import (
    InteractiveRuntimeDiagnostic,
    ProviderSessionDiagnostic,
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
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshCause,
    MetricsRefreshDiagnostic,
)


def doctor_json(
    result: DoctorResult,
    *,
    runtime: InteractiveRuntimeDiagnostic | None = None,
) -> JsonObject:
    """Build recursively typed Doctor JSON from one completed result."""
    runtime = (
        InteractiveRuntimeDiagnostic.unavailable()
        if runtime is None
        else runtime
    )
    accounts: JsonValue
    persistence: JsonObject
    if isinstance(result, DoctorReadyResult):
        accounts = [
            _diagnostic_dict(diagnostic) for diagnostic in result.diagnostics
        ]
        persistence = _persistence_dict(result.persistence)
        persistence["credential_refresh"] = result.refresh_state.kind.value
    elif isinstance(result, DoctorFailedResult):
        accounts = ServiceComponentState.UNAVAILABLE.value
        persistence = _persistence_failure_dict(result.failure)
    else:
        assert_never(result)
    return {
        "accounts": accounts,
        "provider_capabilities": _capability_dicts(result.capabilities),
        "service": _service_dict(result.supervisor),
        "sessions": _session_dict(runtime),
        "operations": _operation_dict(
            result.supervisor,
            (
                result.scheduled_operations
                if isinstance(result, DoctorReadyResult)
                else ServiceComponentState.UNAVAILABLE
            ),
            (
                result.unfinished_activations
                if isinstance(result, DoctorReadyResult)
                else ServiceComponentState.UNAVAILABLE
            ),
        ),
        "persistence": persistence,
        "metrics_refresh": _metrics_refresh_dict(result.metrics_refresh),
    }


def _metrics_refresh_dict(
    diagnostic: MetricsRefreshDiagnostic,
) -> JsonObject:
    """Build one global sanitized metrics-refresh diagnostic."""
    observation = diagnostic.observation
    return {
        "state": diagnostic.state.value,
        "observed_at": (
            None
            if observation is None
            else canonical_timestamp(observation.observed_at)
        ),
        "outcome": None if observation is None else observation.outcome.value,
        "attempts": None if observation is None else observation.attempts,
        "retry_causes": (
            []
            if observation is None
            else [
                _metrics_refresh_cause_dict(cause)
                for cause in observation.retry_causes
            ]
        ),
        "causes": (
            []
            if observation is None
            else [
                _metrics_refresh_cause_dict(cause)
                for cause in observation.causes
            ]
        ),
    }


def _metrics_refresh_cause_dict(
    cause: MetricsRefreshCause,
) -> JsonObject:
    """Build one bounded metrics-refresh cause."""
    return {
        "stage": cause.stage.value,
        "code": cause.code.value,
        "provider": (
            None if cause.provider_id is None else cause.provider_id.value
        ),
        "account_id": (
            None if cause.account_id is None else str(cause.account_id)
        ),
    }


def _service_dict(health: SupervisorHealth) -> JsonObject:
    """Build machine-readable resident-service health."""
    preparation = health.broker_preparation_report
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
        "protocol_version": PROTOCOL_VERSION,
        "broker": health.broker.value,
        "broker_failure_code": health.broker_failure_code,
        "broker_preparation_report": (
            None
            if preparation is None
            else {
                "dry_run": preparation.dry_run,
                "operator_steps": list(preparation.operator_steps),
                "reason": preparation.reason,
            }
        ),
    }


def _session_dict(runtime: InteractiveRuntimeDiagnostic) -> JsonObject:
    """Build safe shell and provider-session diagnostics."""
    return {
        "selection_status": runtime.selection_status.value,
        "shell_integration": runtime.shell_integration_code,
        "providers": [
            _provider_session_dict(provider) for provider in runtime.providers
        ],
    }


def _provider_session_dict(
    diagnostic: ProviderSessionDiagnostic,
) -> JsonObject:
    """Build one participant-free provider session diagnostic."""
    return {
        "provider": diagnostic.provider_id.value,
        "finalized_account_id": (
            None
            if diagnostic.finalized_account_id is None
            else str(diagnostic.finalized_account_id)
        ),
        "finalized_epoch": (
            None
            if diagnostic.finalized_epoch is None
            else diagnostic.finalized_epoch.value
        ),
        "target_account_id": (
            None
            if diagnostic.target_account_id is None
            else str(diagnostic.target_account_id)
        ),
        "pending_epoch": (
            None
            if diagnostic.pending_epoch is None
            else diagnostic.pending_epoch.value
        ),
        "phase": None if diagnostic.phase is None else diagnostic.phase.value,
        "code": None if diagnostic.code is None else diagnostic.code.value,
        "registered": diagnostic.registered_count,
        "reachable": diagnostic.reachable_count,
        "required": diagnostic.required_count,
        "ready": diagnostic.ready_count,
        "adopted": diagnostic.adopted_count,
        "unreachable": diagnostic.unreachable_count,
        "confirmed_dead_after_commit": (
            diagnostic.confirmed_dead_after_commit_count
        ),
        "active_turns": diagnostic.active_turn_count,
        "queued_turns": diagnostic.queued_turn_count,
        "unmanaged": diagnostic.unmanaged_count,
        "session_enrollment": diagnostic.session_enrollment,
        "codex_effective_config": (
            diagnostic.protected_session_state
            if diagnostic.provider_id is ProviderId.CODEX
            else None
        ),
        "claude_structured_host": (
            diagnostic.protected_session_state
            if diagnostic.provider_id is ProviderId.CLAUDE
            else None
        ),
    }


def _operation_dict(
    health: SupervisorHealth,
    scheduled: (
        tuple[ScheduledOperationDiagnostic, ...] | ServiceComponentState
    ),
    activations: (
        tuple[UnfinishedActivationDiagnostic, ...] | ServiceComponentState
    ),
) -> JsonObject:
    """Build machine-readable queue and journal health."""
    scheduled_value: JsonValue = (
        scheduled.value
        if isinstance(scheduled, ServiceComponentState)
        else [_scheduled_operation_dict(operation) for operation in scheduled]
    )
    activation_value: JsonValue = (
        activations.value
        if isinstance(activations, ServiceComponentState)
        else [
            _unfinished_activation_dict(activation)
            for activation in activations
        ]
    )
    return {
        "queue": health.queue.value,
        "journal": health.journal.value,
        "scheduled": scheduled_value,
        "unfinished_activations": activation_value,
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
        capabilities = {
            "schema_hash": result.capabilities.schema_hash,
            "session_schema_supported": (
                result.capabilities.session_schema_supported
            ),
        }
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
