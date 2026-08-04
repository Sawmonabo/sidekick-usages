"""Provider-specific recovery policy for resident Codex authority."""

from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionAuthorityObservation,
    SelectionRecoveryDecision,
)
from sidekick_usages.core.selection.policy import (
    selection_recovery_decision,
)
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
    SelectionCode,
    SelectionRecoveryRelation,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.auth.generation import (
    codex_generation_relation,
)


def codex_recovery_decision(
    operation: OpenSelectionOperation,
    baseline: FinalizedSelection | None,
    observation: SelectionAuthorityObservation,
    *,
    target_binding_proven: bool,
) -> SelectionRecoveryDecision:
    """Relate Codex runtime and exact participant authority proof."""
    decision = selection_recovery_decision(
        operation,
        baseline,
        observation,
        target_binding_proven=target_binding_proven,
        baseline_observation_conclusive=True,
    )
    if (
        decision.relation is not SelectionRecoveryRelation.UNRESOLVED
        or baseline is None
        or operation.baseline_account_id == operation.target_account_id
        or observation.provider_id is not ProviderId.CODEX
        or observation.account_id != operation.baseline_account_id
        or observation.generation is None
    ):
        return decision
    try:
        generation_relation = codex_generation_relation(
            baseline.generation,
            observation.generation,
        )
    except ValueError:
        return decision
    if generation_relation is AuthorityGenerationRelation.OLDER:
        return decision
    return SelectionRecoveryDecision(
        relation=SelectionRecoveryRelation.BASELINE_PROVEN,
        target_generation=None,
        safe_code=SelectionCode.SELECTION_ROLLED_BACK,
    )
