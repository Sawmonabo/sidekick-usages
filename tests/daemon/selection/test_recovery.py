"""Forward-only global selection recovery tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.policy import selection_recovery_decision
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationState,
    SelectionOutcome,
    SelectionPhase,
    SelectionRecoveryRelation,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import SelectionWorkerMetadata
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from sidekick_usages.providers.codex.session.quiescence import (
    CodexParticipantProofSet,
)
from tests.support.persistence import (
    make_application_paths,
    seed_finalized_selections,
)
from tests.support.time import REFERENCE_TIME, FixedClock

PROVIDER_ID = ProviderId.CLAUDE
OPERATION_ID = OperationId("52bbb5ad-b457-41ce-90ca-c52919051f8e")
REPLAY_OPERATION_ID = OperationId("25c10782-ae80-4f9b-a6fa-65bd029a4934")
TARGET_ACCOUNT_ID = SidekickAccountId("32b53411-10ef-4689-a5ea-6ec9daec4e2b")
TARGET_GENERATION = AuthorityGeneration("generation-target-8")


class _ForbiddenSelectionAdapter:
    """Reject provider work in an already-committed recovery journey."""

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        del operation, baseline
        raise AssertionError("Recovery repeated provider prevalidation.")

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        del prepared
        raise AssertionError("Recovery repeated provider commit.")

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> SelectionAuthorityObservation:
        del prepared
        raise AssertionError("Recovery bypassed its retained readback.")


def _baseline_selection() -> FinalizedSelection:
    return FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=SidekickAccountId("4d95b1fb-1ba4-4837-b6b7-452cd8aff462"),
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-baseline-7"),
        finalized_at=REFERENCE_TIME,
    )


def _completion(
    operation_id: OperationId,
    kind: OperationKind,
    account_id: SidekickAccountId,
    generation: AuthorityGeneration,
    epoch: SelectionEpoch,
) -> SchedulerCompletion:
    return SchedulerCompletion(
        PROVIDER_ID,
        operation_id,
        kind,
        None,
        WorkerOutcome.SUCCEEDED,
        None,
        selection=SelectionWorkerMetadata(
            operation_id=operation_id,
            provider_id=PROVIDER_ID,
            kind=kind,
            pending_epoch=epoch,
            observed_account_id=account_id,
            observed_generation=generation,
            authority_requires_participant=True,
        ),
    )


@pytest.mark.parametrize("baseline_exists", [False, True])
def test_recovery_finalizes_forward_from_target_provider_proof(
    tmp_path: Path,
    baseline_exists: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover forward and reject unrelated same-account generations."""
    paths = make_application_paths(tmp_path)
    baseline = _baseline_selection() if baseline_exists else None
    if baseline is not None:
        seed_finalized_selections(paths, baseline)
    baseline_epoch = SelectionEpoch(0) if baseline is None else baseline.epoch
    target_account_id = (
        TARGET_ACCOUNT_ID if baseline is None else baseline.account_id
    )
    selected = SelectedStateStore(paths.selected_state)
    journal = SelectionOperationStore(paths.selection_journals)
    operation = OpenSelectionOperation(
        operation_id=OPERATION_ID,
        provider_id=PROVIDER_ID,
        baseline_account_id=None if baseline is None else baseline.account_id,
        target_account_id=target_account_id,
        prepared_generation=None,
        target_generation=None,
        baseline_epoch=baseline_epoch,
        pending_epoch=baseline_epoch.next(),
        phase=SelectionPhase.PREVALIDATING,
        required_participant_ids=(),
        ready_participant_ids=(),
        lost_after_commit_participant_ids=(),
        confirmed_dead_before_commit_count=0,
        confirmed_dead_before_commit_code=None,
        outcome_code=None,
        started_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    journal.begin(operation)
    for phase in (
        SelectionPhase.PREPARING,
        SelectionPhase.WAITING_OLD_TURNS,
        SelectionPhase.COMMITTING,
    ):
        replacement = replace(
            operation,
            phase=phase,
            prepared_generation=(
                TARGET_GENERATION
                if baseline is not None
                else AuthorityGeneration("generation-source-8")
            ),
        )
        journal.compare_and_swap(operation, replacement)
        operation = replacement
    registry = ParticipantRegistry(selected)
    prepared_generations: list[AuthorityGeneration] = []
    prepare_target = registry.prepare_target

    def record_target(
        operation_id: OperationId,
        proof: AuthorityReadyProof,
    ) -> bool:
        prepared_generations.append(proof.generation)
        return prepare_target(operation_id, proof)

    monkeypatch.setattr(registry, "prepare_target", record_target)
    queue = OperationQueueStore(paths.durable_operations)
    recovery = SelectionRecovery(
        selected,
        journal,
        registry,
        SelectionWorkerGateway(queue, FixedClock(), lambda: None),
        FixedClock(),
    )
    assert recovery.restore(PROVIDER_ID)
    (readback,) = recovery.enqueue_restored_readbacks()
    assert readback.selection_operation_id == operation.operation_id
    recovery.fail_readback(readback, "selection_recovery_required")
    queue.remove(
        readback.operation_id,
        expected_state=OperationState.SCHEDULED,
    )
    if baseline is not None:
        unrelated = AuthorityGeneration("generation-unrelated-9")
        classified = replace(
            operation,
            target_generation=TARGET_GENERATION,
        )
        relations = tuple(
            selection_recovery_decision(
                classified,
                baseline,
                SelectionAuthorityObservation(
                    provider_id=PROVIDER_ID,
                    account_id=target_account_id,
                    generation=generation,
                ),
                target_binding_proven=False,
                baseline_observation_conclusive=True,
            ).relation
            for generation in (
                baseline.generation,
                TARGET_GENERATION,
                unrelated,
            )
        )
        assert relations == (
            SelectionRecoveryRelation.BASELINE_PROVEN,
            SelectionRecoveryRelation.TARGET_PROVEN,
            SelectionRecoveryRelation.UNRESOLVED,
        )
        codex_baseline = replace(
            baseline,
            provider_id=ProviderId.CODEX,
            generation=AuthorityGeneration("2026-08-04T03:38:35.101902541Z"),
        )
        codex_operation = replace(
            operation,
            provider_id=ProviderId.CODEX,
            baseline_account_id=codex_baseline.account_id,
            target_account_id=TARGET_ACCOUNT_ID,
        )
        refreshed_baseline = CodexParticipantProofSet.recovery_decision(
            codex_operation,
            codex_baseline,
            SelectionAuthorityObservation(
                provider_id=ProviderId.CODEX,
                account_id=codex_baseline.account_id,
                generation=AuthorityGeneration(
                    "2026-08-04T05:29:38.756428940Z"
                ),
            ),
            target_binding_proven=False,
        )
        assert refreshed_baseline.relation is (
            SelectionRecoveryRelation.BASELINE_PROVEN
        )
        recovery.complete_readback(
            _completion(
                operation.operation_id,
                readback.kind,
                target_account_id,
                unrelated,
                operation.pending_epoch,
            )
        )
        assert prepared_generations == []
    completion = _completion(
        operation.operation_id,
        readback.kind,
        target_account_id,
        TARGET_GENERATION,
        operation.pending_epoch,
    )
    coordinator = SelectionCoordinator(
        selected,
        journal,
        registry,
        _ForbiddenSelectionAdapter(),
        FixedClock(),
        resume_recovery=lambda provider_id: recovery.complete_readback(
            completion
        ),
    )
    _, stream = coordinator.select_events(
        REPLAY_OPERATION_ID,
        PROVIDER_ID,
        target_account_id,
    )
    *_, result = stream
    assert isinstance(result, SelectionResult)
    (finalized,) = selected.load_all()
    provider_journal = journal.load(PROVIDER_ID)
    assert (
        finalized.account_id,
        finalized.epoch,
        result.outcome,
        provider_journal.active,
        set(prepared_generations),
    ) == (
        target_account_id,
        baseline_epoch.next(),
        SelectionOutcome.READY,
        None,
        {TARGET_GENERATION},
    )
