"""Pure transition policy for provider selection and durable operations."""

from dataclasses import replace
from datetime import datetime

from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    ProviderAuthObservation,
    SelectedAccountState,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionRecoveryDecision,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    SelectionRecoveryRelation,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId

_PROTECTED_SELECTION_PROVIDERS: frozenset[ProviderId] = frozenset(
    {ProviderId.CLAUDE, ProviderId.CODEX}
)

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


def protected_selection_enabled(provider_id: ProviderId) -> bool:
    """Return whether protected selection is released for one provider."""
    return provider_id in _PROTECTED_SELECTION_PROVIDERS


def selection_recovery_decision(
    operation: OpenSelectionOperation,
    baseline: FinalizedSelection | None,
    observation: SelectionAuthorityObservation,
    *,
    target_binding_proven: bool,
    baseline_observation_conclusive: bool,
) -> SelectionRecoveryDecision:
    """Relate composite provider evidence without guessing rollback."""
    target_generation = operation.target_generation
    if operation.baseline_account_id == operation.target_account_id:
        expected_target = target_generation or operation.prepared_generation
        if (
            expected_target is not None
            and observation.provider_id is operation.provider_id
            and observation.account_id == operation.target_account_id
            and observation.generation == expected_target
        ):
            return SelectionRecoveryDecision(
                relation=SelectionRecoveryRelation.TARGET_PROVEN,
                target_generation=expected_target,
                safe_code=SelectionCode.SELECTION_SUCCEEDED,
            )
        if baseline_observation_conclusive and _selection_baseline_proven(
            operation,
            baseline,
            observation,
        ):
            return SelectionRecoveryDecision(
                relation=SelectionRecoveryRelation.BASELINE_PROVEN,
                target_generation=None,
                safe_code=SelectionCode.SELECTION_ROLLED_BACK,
            )
        return SelectionRecoveryDecision(
            relation=SelectionRecoveryRelation.UNRESOLVED,
            target_generation=None,
            safe_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
        )
    observed_target = (
        observation.provider_id is operation.provider_id
        and observation.account_id == operation.target_account_id
        and observation.generation is not None
        and (
            target_generation is None
            or target_generation == observation.generation
        )
    )
    if observed_target:
        target_generation = observation.generation
    if target_binding_proven and target_generation is None:
        target_generation = operation.prepared_generation
    if target_generation is not None:
        return SelectionRecoveryDecision(
            relation=SelectionRecoveryRelation.TARGET_PROVEN,
            target_generation=target_generation,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )
    if baseline_observation_conclusive and _selection_baseline_proven(
        operation,
        baseline,
        observation,
    ):
        return SelectionRecoveryDecision(
            relation=SelectionRecoveryRelation.BASELINE_PROVEN,
            target_generation=None,
            safe_code=SelectionCode.SELECTION_ROLLED_BACK,
        )
    return SelectionRecoveryDecision(
        relation=SelectionRecoveryRelation.UNRESOLVED,
        target_generation=None,
        safe_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
    )


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
        or not _selection_prepared_generation_transition(
            expected,
            replacement,
        )
        or not _selection_target_generation_transition(
            expected,
            replacement,
        )
        or not _selection_required_transition(expected, replacement)
        or not _selection_ready_transition(expected, replacement)
        or not _selection_lost_transition(expected, replacement)
    ):
        raise ValueError("Illegal global selection transition.")
    return replacement


def selection_result_matches_finalized(
    operation: OpenSelectionOperation,
    result: SelectionResult,
    finalized: FinalizedSelection | None,
) -> bool:
    """Return whether one terminal result matches durable selection truth."""
    if (
        result.operation_id != operation.operation_id
        or result.provider_id is not operation.provider_id
        or result.target_account_id != operation.target_account_id
    ):
        return False
    if result.outcome in {
        SelectionOutcome.READY,
        SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
    }:
        return (
            finalized is not None
            and result.target_generation is not None
            and finalized.provider_id is operation.provider_id
            and finalized.account_id == result.target_account_id
            and finalized.epoch == result.epoch == operation.pending_epoch
            and finalized.generation == result.target_generation
        )
    if result.outcome is not SelectionOutcome.FAILED_OLD_EPOCH:
        return False
    if result.epoch != operation.baseline_epoch:
        return False
    if operation.baseline_account_id is None:
        return finalized is None
    return (
        finalized is not None
        and finalized.provider_id is operation.provider_id
        and finalized.account_id == operation.baseline_account_id
        and finalized.epoch == operation.baseline_epoch
    )


def _selection_baseline_proven(
    operation: OpenSelectionOperation,
    baseline: FinalizedSelection | None,
    observation: SelectionAuthorityObservation,
) -> bool:
    """Return whether native evidence exactly proves the old authority."""
    if baseline is None:
        return (
            operation.baseline_account_id is None
            and operation.baseline_epoch == SelectionEpoch(0)
            and observation.provider_id is operation.provider_id
            and observation.account_id is None
        )
    return (
        baseline.account_id == operation.baseline_account_id
        and baseline.epoch == operation.baseline_epoch
        and observation.provider_id is operation.provider_id
        and observation.account_id == baseline.account_id
        and observation.generation == baseline.generation
    )


def _selection_prepared_generation_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow prepared source generation to be learned exactly once."""
    if expected.phase is SelectionPhase.PREVALIDATING:
        return (
            expected.prepared_generation is None
            and replacement.phase is SelectionPhase.PREPARING
            and replacement.prepared_generation is not None
        )
    return replacement.prepared_generation == expected.prepared_generation


def _selection_target_generation_transition(
    expected: OpenSelectionOperation,
    replacement: OpenSelectionOperation,
) -> bool:
    """Allow exact runtime generation to be learned once after commit."""
    if expected.target_generation is None:
        return replacement.target_generation is None or (
            expected.phase
            in {SelectionPhase.COMMITTING, SelectionPhase.RECOVERING}
            and replacement.phase
            in {
                SelectionPhase.COMMITTING,
                SelectionPhase.AWAITING_READY,
            }
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
    )
