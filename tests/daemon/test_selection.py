"""Durable global-selection epoch and recovery tests."""

import stat
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
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
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME

PROVIDER_ID = ProviderId.CLAUDE
OPERATION_ID = OperationId("52bbb5ad-b457-41ce-90ca-c52919051f8e")
TARGET_ACCOUNT_ID = SidekickAccountId("32b53411-10ef-4689-a5ea-6ec9daec4e2b")
PARTICIPANT_A = ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337")
PARTICIPANT_B = ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc")
SECRET_CANARY = b"synthetic-secret-must-never-be-persisted"
AUTHORITY_CANARY = b"synthetic-provider-authority-must-remain-unchanged"
PRIVATE_FILE_MODE = 0o600
MAX_SELECTION_EPOCH = 2**63 - 1
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
    """Build one unsorted, secret-free operation for round-trip proof."""
    return OpenSelectionOperation(
        operation_id=OPERATION_ID,
        provider_id=PROVIDER_ID,
        target_account_id=TARGET_ACCOUNT_ID,
        target_generation=AuthorityGeneration("generation-target-7"),
        baseline_epoch=SelectionEpoch(7),
        pending_epoch=SelectionEpoch(8),
        phase=SelectionPhase.PREVALIDATING,
        required_participant_ids=(PARTICIPANT_B, PARTICIPANT_A),
        ready_participant_ids=(),
        adopted_participant_ids=(),
        started_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )


def _waiting_old_turns(
    operation: OpenSelectionOperation,
) -> OpenSelectionOperation:
    """Advance one operation to its old-turn drain barrier."""
    return replace(
        operation,
        phase=SelectionPhase.WAITING_OLD_TURNS,
    )


def _awaiting_ready(
    operation: OpenSelectionOperation,
) -> OpenSelectionOperation:
    """Advance one committed operation to participant readiness."""
    return replace(
        operation,
        phase=SelectionPhase.AWAITING_READY,
        ready_participant_ids=(PARTICIPANT_A,),
    )


def _degraded_target_result(
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
        required_count=2,
        ready_count=1,
        adopted_count=0,
        lost_count=1,
        started_at=operation.started_at,
        completed_at=operation.updated_at,
    )


def _persisted_selection_bytes(root: Path) -> bytes:
    """Read every regular selection artifact below one isolated root."""
    return b"".join(
        path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
    )


@pytest.mark.parametrize(
    "crash_after_write",
    [None, 0, 1, 2, 3],
    ids=("no-crash", "begin", "waiting", "awaiting", "complete"),
)
def test_selection_journal_is_forward_only_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_write: int | None,
) -> None:
    """Each durable phase recovers forward without secret persistence."""
    paths = make_application_paths(tmp_path)
    operation = _open_selection_operation()
    waiting = _waiting_old_turns(operation)
    awaiting = _awaiting_ready(waiting)
    result = _degraded_target_result(awaiting)
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
        (lambda: store.compare_and_swap(operation, waiting), waiting, None),
        (lambda: store.compare_and_swap(waiting, awaiting), awaiting, None),
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

    with pytest.raises(ManagedStateConflictError):
        store.compare_and_swap(awaiting, waiting)

    document = store.load(PROVIDER_ID)
    assert document.active is None
    assert document.history[-1].lost_count == 1
    assert operation.required_participant_ids == (
        PARTICIPANT_A,
        PARTICIPANT_B,
    )
    journal = paths.selection_journals / f"{PROVIDER_ID.value}.json"
    assert stat.S_IMODE(journal.stat().st_mode) == PRIVATE_FILE_MODE
    assert SECRET_CANARY not in _persisted_selection_bytes(
        paths.selection_journals
    )
    invalid_begin = SelectionOperationStore(
        paths.selection_journals / "invalid-begin"
    )
    with pytest.raises(ValueError, match="prevalidating"):
        invalid_begin.begin(waiting)
    invalid_completion = SelectionOperationStore(
        paths.selection_journals / "invalid-completion"
    )
    invalid_completion.begin(operation)
    with pytest.raises(ManagedStateConflictError):
        invalid_completion.complete(result)


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
    with pytest.raises(ManagedStateConflictError):
        store.compare_and_swap(
            replace(expected, epoch=SelectionEpoch(2)),
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

    assert SelectedStateStore(paths.selected_state).load_all() == (expected,)
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
