"""Pure transition policy for provider selection and durable operations."""

from dataclasses import replace
from datetime import datetime

from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ActivationRecoveryAction,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.time import as_utc

__all__ = [
    "coalesce_due_operation",
    "decide_activation_recovery",
    "transition_activation",
    "transition_operation",
]

_ACTIVATION_TRANSITIONS: dict[
    ActivationPhase,
    frozenset[ActivationPhase],
] = {
    ActivationPhase.PREPARED: frozenset(
        {
            ActivationPhase.OUTGOING_RETAINED,
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.OUTGOING_RETAINED: frozenset(
        {
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.TARGET_ACTIVATED: frozenset(
        {
            ActivationPhase.READ_BACK_VERIFIED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.READ_BACK_VERIFIED: frozenset(
        {
            ActivationPhase.COMMITTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.COMMITTED: frozenset(),
    ActivationPhase.ROLLED_BACK: frozenset(),
    ActivationPhase.RECONCILIATION_REQUIRED: frozenset(),
}
_OPERATION_TRANSITIONS: dict[
    OperationState,
    frozenset[OperationState],
] = {
    OperationState.SCHEDULED: frozenset({OperationState.RUNNING}),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SCHEDULED,
            OperationState.RETRY_WAIT,
            OperationState.ACTION_REQUIRED,
        }
    ),
    OperationState.RETRY_WAIT: frozenset(
        {OperationState.RUNNING, OperationState.SCHEDULED}
    ),
    OperationState.ACTION_REQUIRED: frozenset({OperationState.SCHEDULED}),
}


def transition_activation(
    record: ActivationRecord,
    phase: ActivationPhase,
    *,
    updated_at: datetime,
    outcome: ActivationOutcome | None = None,
    failure_code: str | None = None,
) -> ActivationRecord:
    """Advance one activation through exactly one legal phase edge."""
    if phase not in _ACTIVATION_TRANSITIONS[record.phase]:
        raise ValueError("Illegal activation phase transition.")
    normalized_update = max(as_utc(updated_at), record.updated_at)
    defaults = {
        ActivationPhase.COMMITTED: ActivationOutcome.VERIFIED,
        ActivationPhase.ROLLED_BACK: ActivationOutcome.ROLLED_BACK,
        ActivationPhase.RECONCILIATION_REQUIRED: (
            ActivationOutcome.RECONCILIATION_REQUIRED
        ),
    }
    effective_outcome = outcome
    if effective_outcome is None:
        effective_outcome = defaults.get(phase)
    return replace(
        record,
        phase=phase,
        updated_at=normalized_update,
        outcome=effective_outcome,
        failure_code=failure_code,
    )


def decide_activation_recovery(
    record: ActivationRecord,
    read_back: SelectedAccountState,
) -> ActivationRecoveryAction:
    """Choose recovery from actual provider state, never journal preference."""
    if record.provider_id is not read_back.provider_id:
        raise ValueError("Activation and provider read-back do not match.")
    if read_back.runtime_state in {
        ProviderRuntimeState.UNREADABLE,
        ProviderRuntimeState.UNSUPPORTED,
    }:
        action = ActivationRecoveryAction.RECONCILIATION_REQUIRED
    elif (
        read_back.provider_identity == record.expected_target_identity
        and read_back.runtime_state
        in {
            ProviderRuntimeState.SAVED_ACTIVE,
            ProviderRuntimeState.EXTERNAL_ACTIVE,
        }
    ):
        action = ActivationRecoveryAction.COMMIT_VERIFIED
    elif (
        record.source_provider_identity is not None
        and read_back.provider_identity == record.source_provider_identity
    ):
        action = ActivationRecoveryAction.ROLLBACK_VERIFIED
    elif read_back.runtime_state in {
        ProviderRuntimeState.SAVED_ACTIVE,
        ProviderRuntimeState.EXTERNAL_ACTIVE,
    }:
        action = ActivationRecoveryAction.RECONCILE_EXTERNAL
    elif record.source_provider_identity is not None:
        action = ActivationRecoveryAction.REQUEST_OFFICIAL_ROLLBACK
    elif record.phase in {
        ActivationPhase.PREPARED,
        ActivationPhase.OUTGOING_RETAINED,
    }:
        action = ActivationRecoveryAction.CLOSE_FAILED
    else:
        action = ActivationRecoveryAction.RECONCILIATION_REQUIRED
    return action


def transition_operation(
    operation: DueOperation,
    state: OperationState,
    *,
    updated_at: datetime,
    due_at: datetime | None = None,
    failure_code: str | None = None,
) -> DueOperation:
    """Advance one durable operation through one legal lifecycle edge."""
    if state not in _OPERATION_TRANSITIONS[operation.state]:
        raise ValueError("Illegal durable operation transition.")
    normalized_update = max(as_utc(updated_at), operation.updated_at)
    effective_due_at = operation.due_at if due_at is None else as_utc(due_at)
    attempts = operation.attempts
    if state is OperationState.RUNNING:
        attempts += 1
    elif state is OperationState.SCHEDULED:
        attempts = 0
        failure_code = None
    return replace(
        operation,
        state=state,
        due_at=effective_due_at,
        updated_at=normalized_update,
        attempts=attempts,
        failure_code=failure_code,
    )


def coalesce_due_operation(
    current: DueOperation,
    incoming: DueOperation,
) -> DueOperation:
    """Coalesce one duplicate slot without appending unbounded work."""
    if (
        current.provider_id is not incoming.provider_id
        or current.account_id != incoming.account_id
        or current.kind is not incoming.kind
    ):
        raise ValueError("Only the same durable operation slot can coalesce.")
    if incoming.state is not OperationState.SCHEDULED:
        raise ValueError("Incoming due work must be scheduled.")
    if current.state is OperationState.RUNNING:
        return current
    if (
        current.state is OperationState.ACTION_REQUIRED
        and incoming.priority is OperationPriority.SCHEDULED
    ):
        return current
    if (
        current.state is OperationState.RETRY_WAIT
        and incoming.priority is OperationPriority.SCHEDULED
    ):
        return current
    priority = (
        incoming.priority
        if incoming.priority.rank < current.priority.rank
        else current.priority
    )
    return replace(
        current,
        priority=priority,
        state=OperationState.SCHEDULED,
        due_at=min(current.due_at, incoming.due_at),
        updated_at=max(current.updated_at, incoming.updated_at),
        failure_code=None,
    )
