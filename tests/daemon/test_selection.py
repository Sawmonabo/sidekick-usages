"""Durable global-selection epoch and recovery tests."""

import stat
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    FinalizedSelection,
    OpenSelectionOperation,
    ProviderAuthObservation,
    ProviderRuntimeSnapshot,
    SelectedAccountState,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.policy import require_selection_transition
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ParticipantId,
    ProviderAuthState,
    ProviderRuntimeState,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.runtime import RuntimeStateReader
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from tests.support.persistence import (
    make_application_paths,
    seed_finalized_selections,
)
from tests.support.time import REFERENCE_TIME, FixedClock

PROVIDER_ID = ProviderId.CLAUDE
OPERATION_ID = OperationId("52bbb5ad-b457-41ce-90ca-c52919051f8e")
TARGET_ACCOUNT_ID = SidekickAccountId("32b53411-10ef-4689-a5ea-6ec9daec4e2b")
PARTICIPANT_A = ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337")
PARTICIPANT_B = ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc")
PARTICIPANT_C = ParticipantId("e9b1b25c-fae6-4998-a135-719ad3257972")
SECRET_CANARY = b"synthetic-secret-must-never-be-persisted"
AUTHORITY_CANARY = b"synthetic-provider-authority-must-remain-unchanged"
PRIVATE_FILE_MODE = 0o600
MAX_SELECTION_EPOCH = 2**63 - 1
LEGAL_SELECTION_EDGES = {
    SelectionPhase.PREVALIDATING: frozenset({SelectionPhase.PREPARING}),
    SelectionPhase.PREPARING: frozenset(
        {
            SelectionPhase.PREPARING,
            SelectionPhase.WAITING_OLD_TURNS,
        }
    ),
    SelectionPhase.WAITING_OLD_TURNS: frozenset(
        {
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.COMMITTING,
        }
    ),
    SelectionPhase.COMMITTING: frozenset(
        {
            SelectionPhase.COMMITTING,
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        }
    ),
    SelectionPhase.AWAITING_READY: frozenset(
        {
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        }
    ),
    SelectionPhase.RECOVERING: frozenset(
        {
            SelectionPhase.RECOVERING,
            SelectionPhase.AWAITING_READY,
        }
    ),
}
VERSION_TWO_SELECTED_STATE = b"""{
  "providers": {
    "claude": {
      "account_id": "32b53411-10ef-4689-a5ea-6ec9daec4e2b",
      "outcome": "verified",
      "provider_identity": "synthetic-claude-identity",
      "runtime_generation": "legacy-generation",
      "runtime_state": "saved_active",
      "verified_at": "2026-06-12T12:34:56.789000Z"
    },
    "codex": {
      "account_id": null,
      "outcome": "external_reconciled",
      "provider_identity": "synthetic-external-identity",
      "runtime_generation": "external-generation",
      "runtime_state": "external_active",
      "verified_at": "2026-06-12T12:34:56.789000Z"
    }
  },
  "schema_version": 2
}
"""


class _InjectedCrash(BaseException):
    """Represent process loss after one exact durable publication."""


def test_selection_epoch_is_bounded_and_monotonic() -> None:
    """Epoch zero advances once and the signed bound fails closed."""
    assert SelectionEpoch(0).next() == SelectionEpoch(1)
    maximum = SelectionEpoch(MAX_SELECTION_EPOCH)
    with pytest.raises(ValueError, match="cannot advance"):
        maximum.next()
    for invalid in (-1, MAX_SELECTION_EPOCH + 1, True):
        with pytest.raises(ValueError, match="outside"):
            SelectionEpoch(invalid)


def _open_selection_operation() -> OpenSelectionOperation:
    """Build the exact secret-free prevalidation publication."""
    return OpenSelectionOperation(
        operation_id=OPERATION_ID,
        provider_id=PROVIDER_ID,
        baseline_account_id=None,
        target_account_id=TARGET_ACCOUNT_ID,
        prepared_generation=None,
        target_generation=None,
        baseline_epoch=SelectionEpoch(7),
        pending_epoch=SelectionEpoch(8),
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


def _preparing(
    operation: OpenSelectionOperation,
) -> OpenSelectionOperation:
    """Learn the prepared source generation and capture participants."""
    return replace(
        operation,
        phase=SelectionPhase.PREPARING,
        prepared_generation=AuthorityGeneration("generation-source-7"),
        required_participant_ids=(PARTICIPANT_B, PARTICIPANT_A),
    )


def _selection_result(
    operation: OpenSelectionOperation,
) -> SelectionResult:
    """Close one committed target after a participant is proven lost."""
    return SelectionResult(
        operation_id=operation.operation_id,
        provider_id=operation.provider_id,
        target_account_id=operation.target_account_id,
        target_generation=operation.target_generation,
        epoch=operation.pending_epoch,
        outcome=SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
        safe_code=SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT,
        required_count=len(operation.required_participant_ids),
        ready_count=len(operation.ready_participant_ids),
        adopted_count=0,
        lost_count=len(operation.lost_after_commit_participant_ids),
        started_at=operation.started_at,
        completed_at=operation.updated_at,
    )


def _operation_at_phase(
    phase: SelectionPhase,
) -> OpenSelectionOperation:
    """Build one coherent operation at an exact graph vertex."""
    if phase is SelectionPhase.PREVALIDATING:
        return _open_selection_operation()
    return replace(
        _preparing(_open_selection_operation()),
        phase=phase,
        target_generation=(
            AuthorityGeneration("generation-runtime-8")
            if phase is SelectionPhase.AWAITING_READY
            else None
        ),
        outcome_code=(
            SelectionCode.SELECTION_RECOVERY_REQUIRED
            if phase is SelectionPhase.RECOVERING
            else None
        ),
    )


def _persisted_selection_bytes(root: Path) -> bytes:
    """Read every regular selection artifact below one isolated root."""
    return b"".join(
        path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
    )


def _assert_selection_safety_guards(
    paths: ApplicationPaths,
    store: SelectionOperationStore,
    operation: OpenSelectionOperation,
    preparing: OpenSelectionOperation,
    waiting: OpenSelectionOperation,
    committing: OpenSelectionOperation,
    lost: OpenSelectionOperation,
    result: SelectionResult,
) -> None:
    """Require illegal, terminal, recovery, and secret-safety guards."""
    selection_journals = paths.selection_journals
    illegal = replace(
        operation,
        phase=SelectionPhase.COMMITTING,
        prepared_generation=AuthorityGeneration("generation-source-7"),
    )
    illegal_store = SelectionOperationStore(
        selection_journals / "illegal-transition"
    )
    illegal_store.begin(operation)
    with pytest.raises(ManagedStateConflictError):
        illegal_store.compare_and_swap(operation, illegal)

    document = store.load(PROVIDER_ID)
    assert document.active is None
    assert document.history[-1].lost_count == 1
    assert preparing.required_participant_ids == (
        PARTICIPANT_A,
        PARTICIPANT_B,
    )
    journal = selection_journals / f"{PROVIDER_ID.value}.json"
    assert stat.S_IMODE(journal.stat().st_mode) == PRIVATE_FILE_MODE
    assert SECRET_CANARY not in _persisted_selection_bytes(selection_journals)
    assert all(
        forbidden not in journal.read_bytes()
        for forbidden in (b'"pid"', b'"socket"', b'"address"', b'"path"')
    )
    invalid_begin = SelectionOperationStore(
        selection_journals / "invalid-begin"
    )
    with pytest.raises(ValueError, match="prevalidating"):
        invalid_begin.begin(preparing)
    invalid_completion = SelectionOperationStore(
        selection_journals / "invalid-completion"
    )
    invalid_completion.begin(operation)
    with pytest.raises(ManagedStateConflictError):
        invalid_completion.complete(result)

    prevalidation_failure = SelectionResult(
        operation_id=operation.operation_id,
        provider_id=operation.provider_id,
        target_account_id=operation.target_account_id,
        target_generation=None,
        epoch=operation.baseline_epoch,
        outcome=SelectionOutcome.FAILED_OLD_EPOCH,
        safe_code=SelectionCode.SELECTION_ROLLED_BACK,
        required_count=0,
        ready_count=0,
        adopted_count=0,
        lost_count=0,
        started_at=operation.started_at,
        completed_at=operation.updated_at,
    )
    failed_store = SelectionOperationStore(
        selection_journals / "prevalidation-failure"
    )
    failed_store.begin(operation)
    failed_store.complete(prevalidation_failure)
    assert failed_store.load(PROVIDER_ID).active is None

    recovering = replace(
        lost,
        phase=SelectionPhase.RECOVERING,
        outcome_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
    )
    recovery_required = replace(
        result,
        outcome=SelectionOutcome.RECOVERY_REQUIRED,
        safe_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
    )
    recovery_store = SelectionOperationStore(
        selection_journals / "recovery-required"
    )
    recovery_store.begin(operation)
    recovery_store.compare_and_swap(operation, preparing)
    recovery_store.compare_and_swap(preparing, waiting)
    recovery_store.compare_and_swap(waiting, committing)
    awaiting = replace(
        lost,
        phase=SelectionPhase.AWAITING_READY,
        ready_participant_ids=(),
        lost_after_commit_participant_ids=(),
        outcome_code=None,
    )
    recovery_store.compare_and_swap(committing, awaiting)
    recovery_store.compare_and_swap(awaiting, lost)
    recovery_store.compare_and_swap(lost, recovering)
    recovery_store.complete(recovery_required)
    assert recovery_store.load(PROVIDER_ID).active == recovering


@pytest.mark.parametrize(
    "crash_after_write",
    [None, *range(9)],
    ids=(
        "no-crash",
        "begin",
        "preparing",
        "late",
        "confirmed-dead",
        "waiting",
        "committing",
        "awaiting",
        "lost",
        "complete",
    ),
)
def test_selection_journal_is_forward_only_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_write: int | None,
) -> None:
    """Each durable phase recovers forward without secret persistence."""
    paths = make_application_paths(tmp_path)
    operation = _open_selection_operation()
    preparing = _preparing(operation)
    late = replace(
        preparing,
        required_participant_ids=(
            PARTICIPANT_C,
            PARTICIPANT_A,
            PARTICIPANT_B,
        ),
    )
    confirmed_dead = replace(
        late,
        required_participant_ids=(PARTICIPANT_C, PARTICIPANT_A),
        confirmed_dead_before_commit_count=1,
        confirmed_dead_before_commit_code=(
            SelectionCode.PARTICIPANT_CONFIRMED_DEAD
        ),
    )
    waiting = replace(
        confirmed_dead,
        phase=SelectionPhase.WAITING_OLD_TURNS,
    )
    committing = replace(waiting, phase=SelectionPhase.COMMITTING)
    awaiting = replace(
        committing,
        phase=SelectionPhase.AWAITING_READY,
        target_generation=AuthorityGeneration("generation-runtime-8"),
    )
    lost = replace(
        awaiting,
        ready_participant_ids=(PARTICIPANT_A,),
        lost_after_commit_participant_ids=(PARTICIPANT_C,),
        outcome_code=SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT,
    )
    result = _selection_result(lost)
    store = SelectionOperationStore(paths.selection_journals)
    original_commit = ManagedStateFilesystem.commit_opaque_private
    write_index = 0
    crashed = False

    def crash_after_commit(
        filesystem: ManagedStateFilesystem,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        nonlocal crashed, write_index
        snapshot = original_commit(
            filesystem,
            payload,
            expected_source=expected_source,
        )
        current_index = write_index
        write_index += 1
        if not crashed and crash_after_write == current_index:
            crashed = True
            raise _InjectedCrash
        return snapshot

    monkeypatch.setattr(
        ManagedStateFilesystem,
        "commit_opaque_private",
        crash_after_commit,
    )

    steps = (
        (lambda: store.begin(operation), operation, None),
        (
            lambda: store.compare_and_swap(operation, preparing),
            preparing,
            None,
        ),
        (lambda: store.compare_and_swap(preparing, late), late, None),
        (
            lambda: store.compare_and_swap(late, confirmed_dead),
            confirmed_dead,
            None,
        ),
        (
            lambda: store.compare_and_swap(confirmed_dead, waiting),
            waiting,
            None,
        ),
        (
            lambda: store.compare_and_swap(waiting, committing),
            committing,
            None,
        ),
        (
            lambda: store.compare_and_swap(committing, awaiting),
            awaiting,
            None,
        ),
        (lambda: store.compare_and_swap(awaiting, lost), lost, None),
        (lambda: store.complete(result), None, result),
    )
    for step_index, (persist, expected_active, expected_result) in enumerate(
        steps
    ):
        if crash_after_write == step_index:
            with pytest.raises(_InjectedCrash):
                persist()
            store = SelectionOperationStore(paths.selection_journals)
        else:
            persist()
        recovered = store.load(PROVIDER_ID)
        assert recovered.active == expected_active
        if expected_result is not None:
            assert recovered.history[-1] == expected_result

    if crash_after_write is None:
        _assert_selection_safety_guards(
            paths,
            store,
            operation,
            preparing,
            waiting,
            committing,
            lost,
            result,
        )


@pytest.mark.parametrize(
    "expected_phase",
    tuple(SelectionPhase),
)
def test_selection_transition_graph_has_only_exact_legal_edges(
    expected_phase: SelectionPhase,
) -> None:
    """Every listed edge succeeds and every other phase jump fails."""
    expected = _operation_at_phase(expected_phase)
    allowed = LEGAL_SELECTION_EDGES[expected_phase]
    for replacement_phase in allowed:
        replacement = _operation_at_phase(replacement_phase)
        if expected_phase is SelectionPhase.AWAITING_READY:
            replacement = replace(
                replacement,
                target_generation=expected.target_generation,
            )
        assert (
            require_selection_transition(expected, replacement) == replacement
        )
    for illegal_phase in set(SelectionPhase) - allowed:
        with pytest.raises(ValueError, match="Illegal global selection"):
            require_selection_transition(
                expected,
                _operation_at_phase(illegal_phase),
            )


def test_selected_state_v2_migrates_only_saved_authority(
    tmp_path: Path,
) -> None:
    """Migration snapshots v2 and drops ambient provider pseudo-state."""
    paths = make_application_paths(tmp_path)
    authority_path = paths.private_credentials / "provider-authority.bin"
    PrivateFilesystem(authority_path).commit_opaque_private(AUTHORITY_CANARY)
    PrivateFilesystem(paths.selected_state).commit_opaque_private(
        VERSION_TWO_SELECTED_STATE
    )
    store = SelectedStateStore(paths.selected_state)
    expected = FinalizedSelection(
        provider_id=ProviderId.CLAUDE,
        account_id=TARGET_ACCOUNT_ID,
        epoch=SelectionEpoch(0),
        generation=AuthorityGeneration("legacy-generation"),
        finalized_at=REFERENCE_TIME,
    )

    assert store.load_all() == (expected,)
    assert store.load(ProviderId.CLAUDE) == expected
    assert store.load(ProviderId.CODEX) is None
    assert not hasattr(store, "save")
    advanced = replace(expected, epoch=SelectionEpoch(1))
    assert store.compare_and_swap(advanced, expected=expected) == advanced
    with pytest.raises(ManagedStateConflictError):
        store.compare_and_swap(
            expected,
            expected=expected,
        )
    migrated = paths.selected_state.read_bytes()
    assert b'"schema_version": 3' in migrated
    assert b"synthetic-external-identity" not in migrated
    assert b"external-generation" not in migrated
    snapshots = tuple(
        paths.selected_state.parent.glob(
            f"{paths.selected_state.name}.v2.*.bak"
        )
    )
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == VERSION_TWO_SELECTED_STATE
    assert stat.S_IMODE(snapshots[0].stat().st_mode) == PRIVATE_FILE_MODE
    assert authority_path.read_bytes() == AUTHORITY_CANARY

    assert SelectedStateStore(paths.selected_state).load_all() == (advanced,)
    assert (
        len(
            tuple(
                paths.selected_state.parent.glob(
                    f"{paths.selected_state.name}.v2.*.bak"
                )
            )
        )
        == 1
    )


def test_selected_state_absent_cas_requires_epoch_one(
    tmp_path: Path,
) -> None:
    """Normal publication cannot create an epoch-zero selected pointer."""
    paths = make_application_paths(tmp_path)
    store = SelectedStateStore(paths.selected_state)
    epoch_zero = FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=TARGET_ACCOUNT_ID,
        epoch=SelectionEpoch(0),
        generation=AuthorityGeneration("generation-target-0"),
        finalized_at=REFERENCE_TIME,
    )
    epoch_one = replace(epoch_zero, epoch=SelectionEpoch(1))

    with pytest.raises(ManagedStateConflictError):
        store.compare_and_swap(epoch_zero, expected=None)
    assert store.compare_and_swap(epoch_one, expected=None) == epoch_one


def test_activation_journal_closes_proof_without_finalizing_selection(
    tmp_path: Path,
) -> None:
    """Provider proof closes its journal but cannot allocate an epoch."""
    paths = make_application_paths(tmp_path)
    provider_id = ProviderId.CODEX
    selected = SelectedStateStore(paths.selected_state)
    baseline = FinalizedSelection(
        provider_id=provider_id,
        account_id=TARGET_ACCOUNT_ID,
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-source-7"),
        finalized_at=REFERENCE_TIME,
    )
    seed_finalized_selections(paths, baseline)
    journals = ActivationJournalStore(
        paths.activation_journals,
        paths.durable_operations,
    )
    target_identity = ProviderIdentity("provider-target")
    target_generation = AuthorityGeneration("generation-target-8")
    observation = ProviderAuthObservation(
        provider_id=provider_id,
        state=ProviderAuthState.ACTIVE,
        provider_identity=ProviderIdentity("provider-source"),
        generation=AuthorityGeneration("generation-source-7"),
        observed_at=REFERENCE_TIME,
    )
    with ProviderMutationLock(
        paths.durable_operations,
        provider_id,
        (TARGET_ACCOUNT_ID,),
        timeout_seconds=1.0,
    ).hold() as authority:
        transaction = journals.transaction(
            provider_id,
            (TARGET_ACCOUNT_ID,),
            authority,
        )
        record = transaction.begin(
            ActivationRecord(
                provider_id=provider_id,
                operation_id=OPERATION_ID,
                selected_baseline=None,
                native_auth_baseline=observation,
                target_account_id=TARGET_ACCOUNT_ID,
                expected_target_identity=target_identity,
                target_authority_generation=target_generation,
                phase=ActivationPhase.PREPARED,
                started_at=REFERENCE_TIME,
                updated_at=REFERENCE_TIME,
            )
        )
        record = transaction.advance(
            record.operation_id,
            ActivationPhase.TARGET_ACTIVATED,
            updated_at=REFERENCE_TIME,
        )
        record = transaction.advance(
            record.operation_id,
            ActivationPhase.PROVIDER_PROOF_VERIFIED,
            updated_at=REFERENCE_TIME,
            verified_runtime_generation=target_generation,
        )
        proof = SelectedAccountState(
            provider_id=provider_id,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=TARGET_ACCOUNT_ID,
            provider_identity=target_identity,
            runtime_generation=target_generation,
            verified_at=REFERENCE_TIME,
            outcome=ActivationOutcome.VERIFIED,
        )
        transaction.commit_verified(
            record.operation_id,
            proof,
            updated_at=REFERENCE_TIME,
        )

    assert selected.load(provider_id) == baseline
    document = journals.load(provider_id)
    assert document.active is None
    assert document.history[-1].phase is ActivationPhase.COMMITTED


def test_runtime_snapshot_keeps_selection_and_observations_distinct(
    tmp_path: Path,
) -> None:
    """Runtime reads expose exact facts without inventing their relation."""
    paths = make_application_paths(tmp_path)
    provider_id = ProviderId.CODEX
    finalized = FinalizedSelection(
        provider_id=provider_id,
        account_id=TARGET_ACCOUNT_ID,
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("finalized-generation"),
        finalized_at=REFERENCE_TIME,
    )
    native = ProviderAuthObservation(
        provider_id=provider_id,
        state=ProviderAuthState.ACTIVE,
        provider_identity=ProviderIdentity("native-provider-identity"),
        generation=AuthorityGeneration("ambient-generation"),
        observed_at=REFERENCE_TIME,
    )
    projection = ProviderAuthObservation(
        provider_id=provider_id,
        state=ProviderAuthState.ACTIVE,
        provider_identity=ProviderIdentity("projection-provider-identity"),
        generation=AuthorityGeneration("projection-generation"),
        observed_at=REFERENCE_TIME,
    )
    selected = SelectedStateStore(paths.selected_state)
    seed_finalized_selections(paths, finalized)
    observations = RuntimeAuthObservationStore(paths.durable_operations)
    observations.save_native(native)
    observations.save_projection(projection)
    reader = RuntimeStateReader(
        provider_id,
        selected,
        ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        ),
        OperationQueueStore(paths.durable_operations),
        observations,
        FixedClock(),
    )

    assert reader.current() == ProviderRuntimeSnapshot(
        provider_id=provider_id,
        finalized_selection=finalized,
        native_auth=native,
        projection_auth=projection,
        activation_in_progress=False,
    )
