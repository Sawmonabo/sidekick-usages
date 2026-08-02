"""Pure transition policy for provider selection and durable operations."""

from dataclasses import replace
from datetime import datetime

from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    OpenSelectionOperation,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    SelectionPhase,
)
from sidekick_usages.core.time import as_utc

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
            ActivationPhase.NATIVE_REPAIR_STARTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.TARGET_ACTIVATED: frozenset(
        {
            ActivationPhase.PROVIDER_PROOF_VERIFIED,
            ActivationPhase.NATIVE_REPAIR_STARTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.PROVIDER_PROOF_VERIFIED: frozenset(
        {
            ActivationPhase.NATIVE_REPAIR_STARTED,
            ActivationPhase.COMMITTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.NATIVE_REPAIR_STARTED: frozenset(
        {
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.COMMITTED: frozenset(),
    ActivationPhase.ROLLED_BACK: frozenset(),
    ActivationPhase.RECONCILIATION_REQUIRED: frozenset(
        {
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.PROVIDER_PROOF_VERIFIED,
            ActivationPhase.NATIVE_REPAIR_STARTED,
            ActivationPhase.ROLLED_BACK,
        }
    ),
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
_SELECTION_TRANSITIONS: dict[
    SelectionPhase,
    frozenset[SelectionPhase],
] = {
    SelectionPhase.PREVALIDATING: frozenset({SelectionPhase.PREPARING}),
    SelectionPhase.PREPARING: frozenset(
        {SelectionPhase.PREPARING, SelectionPhase.WAITING_OLD_TURNS}
    ),
    SelectionPhase.WAITING_OLD_TURNS: frozenset(
        {SelectionPhase.WAITING_OLD_TURNS, SelectionPhase.COMMITTING}
    ),
    SelectionPhase.COMMITTING: frozenset(
        {
            SelectionPhase.COMMITTING,
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        }
    ),
    SelectionPhase.AWAITING_READY: frozenset(
        {SelectionPhase.AWAITING_READY, SelectionPhase.RECOVERING}
    ),
    SelectionPhase.RECOVERING: frozenset(
        {SelectionPhase.RECOVERING, SelectionPhase.AWAITING_READY}
    ),
}


def require_selection_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> OpenSelectionOperation:
    """Validate one identity-stable forward-only selection transition."""
    if (
        replacement.operation_id != expected.operation_id
        or replacement.provider_id is not expected.provider_id
        or replacement.baseline_account_id != expected.baseline_account_id
        or replacement.target_account_id != expected.target_account_id
        or replacement.baseline_epoch != expected.baseline_epoch
        or replacement.pending_epoch != expected.pending_epoch
        or replacement.started_at != expected.started_at
        or not set(expected.ready_participant_ids).issubset(
            replacement.ready_participant_ids
        )
        or not set(expected.lost_after_commit_participant_ids).issubset(
            replacement.lost_after_commit_participant_ids
        )
        or replacement.updated_at < expected.updated_at
        or replacement.phase not in _SELECTION_TRANSITIONS[expected.phase]
        or not _selection_generation_transition(expected, replacement)
        or not _selection_required_transition(expected, replacement)
        or not _selection_ready_transition(expected, replacement)
        or not _selection_lost_transition(expected, replacement)
    ):
        raise ValueError("Illegal global selection transition.")
    return replacement


def _selection_generation_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow target generation to be learned exactly once."""
    if expected.phase is SelectionPhase.PREVALIDATING:
        return (
            expected.target_generation is None
            and replacement.phase is SelectionPhase.PREPARING
            and replacement.target_generation is not None
        )
    return replacement.target_generation == expected.target_generation


def _selection_required_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow late additions and only proven precommit removals."""
    before = set(expected.required_participant_ids)
    after = set(replacement.required_participant_ids)
    removed = before - after
    if removed and expected.phase not in {
        SelectionPhase.PREPARING,
        SelectionPhase.WAITING_OLD_TURNS,
    }:
        return False
    expected_count = expected.confirmed_dead_before_commit_count + len(removed)
    if replacement.confirmed_dead_before_commit_count != expected_count:
        return False
    return bool(removed) or (
        replacement.confirmed_dead_before_commit_code
        == expected.confirmed_dead_before_commit_code
    )


def _selection_ready_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow readiness only after a provider commit is proven."""
    if replacement.ready_participant_ids == expected.ready_participant_ids:
        return True
    return replacement.phase in {
        SelectionPhase.AWAITING_READY,
        SelectionPhase.RECOVERING,
    }


def _selection_lost_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow durable loss only during or after provider commit."""
    if (
        replacement.lost_after_commit_participant_ids
        == expected.lost_after_commit_participant_ids
    ):
        return True
    return expected.phase in {
        SelectionPhase.COMMITTING,
        SelectionPhase.AWAITING_READY,
        SelectionPhase.RECOVERING,
    }


def same_provider_auth_authority(
    first: ProviderAuthObservation,
    second: ProviderAuthObservation,
) -> bool:
    """Return whether two observations prove the same provider authority."""
    return (
        first.provider_id is second.provider_id
        and first.state is second.state
        and first.provider_identity == second.provider_identity
        and first.generation == second.generation
    )


def same_selected_runtime_authority(
    first: SelectedAccountState | None,
    second: SelectedAccountState | None,
) -> bool:
    """Return whether two selected states prove the same runtime authority."""
    if first is None or second is None:
        return first is second
    return (
        first.provider_id is second.provider_id
        and first.runtime_state is second.runtime_state
        and first.account_id == second.account_id
        and first.provider_identity == second.provider_identity
        and first.runtime_generation == second.runtime_generation
    )


def transition_activation(
    record: ActivationRecord,
    phase: ActivationPhase,
    *,
    updated_at: datetime,
    verified_runtime_generation: AuthorityGeneration | None = None,
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
    reconciliation_origin = record.reconciliation_origin_phase
    if phase is ActivationPhase.RECONCILIATION_REQUIRED:
        reconciliation_origin = record.phase
    return replace(
        record,
        phase=phase,
        updated_at=normalized_update,
        verified_runtime_generation=verified_runtime_generation,
        outcome=effective_outcome,
        failure_code=failure_code,
        reconciliation_origin_phase=reconciliation_origin,
    )


def transition_operation(
    operation: DueOperation,
    state: OperationState,
    *,
    updated_at: datetime,
    due_at: datetime | None = None,
    failure_code: str | None = None,
    priority: OperationPriority | None = None,
) -> DueOperation:
    """Advance one durable operation through one legal lifecycle edge."""
    if state not in _OPERATION_TRANSITIONS[operation.state]:
        raise ValueError("Illegal durable operation transition.")
    if priority is not None and (
        operation.kind is not OperationKind.RECONCILE_NATIVE
        or state is not OperationState.SCHEDULED
        or priority is not OperationPriority.SCHEDULED
    ):
        raise ValueError(
            "Only recurrent native reconciliation may change priority."
        )
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
        priority=operation.priority if priority is None else priority,
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
        current.kind is OperationKind.MAINTAIN
        and current.state
        in {
            OperationState.ACTION_REQUIRED,
            OperationState.RETRY_WAIT,
        }
        and incoming.priority is OperationPriority.SCHEDULED
    ):
        return incoming
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
    if (
        (
            current.kind is OperationKind.RECONCILE_NATIVE
            and current.priority is OperationPriority.SCHEDULED
        )
        or current.state
        in {
            OperationState.ACTION_REQUIRED,
            OperationState.RETRY_WAIT,
        }
    ) and incoming.priority is OperationPriority.INTERACTIVE:
        return incoming
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
        allow_remote_control_disconnect=(
            current.allow_remote_control_disconnect
            or incoming.allow_remote_control_disconnect
        ),
    )
