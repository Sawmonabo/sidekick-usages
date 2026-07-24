"""Load-bearing durable state and recovery scenarios for the supervisor."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection import (
    ActivationOutcome,
    ActivationPhase,
    ActivationRecord,
    ActivationRecoveryAction,
    DueOperation,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
    SelectedAccountState,
    decide_activation_recovery,
    transition_activation,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_index import AccountIndex
from sidekick_usages.persistence.activation_journal import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.operation_queue import OperationQueueStore
from sidekick_usages.persistence.selected_state import SelectedStateStore
from tests.test_support import (
    REFERENCE_TIME,
    make_application_paths,
    saved_account,
)


def _accounts() -> AccountIndex:
    accounts = (
        saved_account(
            Account(
                label=AccountLabel("claude-source"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-source-secret"
                ),
            )
        ),
        saved_account(
            Account(
                label=AccountLabel("claude-target"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-target-secret"
                ),
            )
        ),
        saved_account(
            Account(
                label=AccountLabel("codex-current"),
                credentials=CodexCredentials(
                    access_token="test-only-codex-secret",
                    account_id="acct-codex",
                ),
            )
        ),
    )
    return AccountIndex(accounts)


def _selected(
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    identity: str,
    generation: str,
    *,
    outcome: ActivationOutcome = ActivationOutcome.VERIFIED,
    verified_in: int = 0,
) -> SelectedAccountState:
    return SelectedAccountState(
        provider_id=provider_id,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        account_id=account_id,
        provider_identity=ProviderIdentity(identity),
        runtime_generation=AuthorityGeneration(generation),
        verified_at=REFERENCE_TIME + timedelta(seconds=verified_in),
        outcome=outcome,
    )


def _operation(
    account: SidekickAccountId,
    provider_id: ProviderId,
    operation_id: str,
    *,
    due_in: int = 0,
) -> DueOperation:
    return DueOperation(
        operation_id=OperationId(operation_id),
        provider_id=provider_id,
        account_id=account,
        kind=OperationKind.MAINTAIN,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME + timedelta(minutes=due_in),
        updated_at=REFERENCE_TIME,
    )


@dataclass(frozen=True, slots=True)
class _FoundationState:
    """One compact synthetic state graph shared by both scenarios."""

    paths: ApplicationPaths
    accounts: AccountIndex
    selected: SelectedStateStore
    journals: ActivationJournalStore
    queue: OperationQueueStore
    operations: tuple[DueOperation, ...]
    codex_state: SelectedAccountState


def _foundation_state(tmp_path: Path) -> _FoundationState:
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.selected_state).repair_parent_permissions()
    accounts = _accounts()
    source, target, codex = tuple(accounts)
    selected = SelectedStateStore(paths.selected_state)
    selected.save(
        _selected(
            ProviderId.CLAUDE,
            source.account_id,
            "claude-source-id",
            "claude-source-generation",
        )
    )
    codex_state = _selected(
        ProviderId.CODEX,
        codex.account_id,
        "codex-account-id",
        "codex-generation",
    )
    selected.save(codex_state)
    operations = (
        _operation(
            source.account_id,
            ProviderId.CLAUDE,
            "806fd66f-591b-4341-b31e-3d25405faf52",
        ),
        _operation(
            target.account_id,
            ProviderId.CLAUDE,
            "cf39e3c5-2517-4c79-937a-4f7d1fe5c916",
        ),
        _operation(
            codex.account_id,
            ProviderId.CODEX,
            "9630cd63-b9c3-4a24-8c78-b8ba4876411b",
        ),
    )
    queue = OperationQueueStore(paths.durable_operations)
    for operation in operations:
        assert queue.enqueue(operation) == operation
    return _FoundationState(
        paths=paths,
        accounts=accounts,
        selected=selected,
        journals=ActivationJournalStore(paths.activation_journals),
        queue=queue,
        operations=operations,
        codex_state=codex_state,
    )


def _activation_record(
    state: _FoundationState,
    operation_id: str,
) -> ActivationRecord:
    source, target, _codex = tuple(state.accounts)
    return ActivationRecord(
        provider_id=ProviderId.CLAUDE,
        operation_id=OperationId(operation_id),
        source_account_id=source.account_id,
        target_account_id=target.account_id,
        source_provider_identity=ProviderIdentity("claude-source-id"),
        source_generation=AuthorityGeneration("claude-source-generation"),
        expected_target_identity=ProviderIdentity("claude-target-id"),
        phase=ActivationPhase.PREPARED,
        started_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )


def test_selection_journal_and_queue_preserve_stable_independent_state(
    tmp_path: Path,
) -> None:
    """A legal switch changes one provider without label or queue coupling."""
    state = _foundation_state(tmp_path)
    source, target, _codex = tuple(state.accounts)
    duplicate = _operation(
        target.account_id,
        ProviderId.CLAUDE,
        "14c50df9-c216-4f99-a88f-4e1a3ab8eb5b",
        due_in=5,
    )
    assert (
        state.queue.enqueue(duplicate).operation_id
        == state.operations[1].operation_id
    )
    record = _activation_record(
        state,
        "fbd44d2b-d774-4328-be10-00b5d3a8650b",
    )
    with state.journals.hold(
        ProviderId.CLAUDE,
        (source.account_id, target.account_id),
    ) as activation:
        activation.begin(record)
        activation.advance(
            record.operation_id,
            ActivationPhase.OUTGOING_RETAINED,
            updated_at=REFERENCE_TIME + timedelta(seconds=1),
        )
        activation.advance(
            record.operation_id,
            ActivationPhase.TARGET_ACTIVATED,
            updated_at=REFERENCE_TIME + timedelta(seconds=2),
        )
        activation.advance(
            record.operation_id,
            ActivationPhase.READ_BACK_VERIFIED,
            updated_at=REFERENCE_TIME + timedelta(seconds=3),
        )
        activation.commit_verified(
            record.operation_id,
            _selected(
                ProviderId.CLAUDE,
                target.account_id,
                "claude-target-id",
                "claude-target-generation",
                verified_in=3,
            ),
            state.selected,
            updated_at=REFERENCE_TIME + timedelta(seconds=4),
        )

    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert state.selected.load(ProviderId.CODEX) == state.codex_state
    assert len(state.queue.load()) == len(state.operations)
    assert state.accounts.rename(
        ProviderId.CLAUDE,
        target.label,
        AccountLabel("claude-renamed"),
    )
    renamed = state.accounts.get(target.account_id)
    assert renamed is not None
    assert renamed.label == "claude-renamed"
    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert (
        state.queue.get(target.account_id, OperationKind.MAINTAIN) is not None
    )
    with pytest.raises(ValueError, match="Illegal activation"):
        transition_activation(
            record,
            ActivationPhase.COMMITTED,
            updated_at=REFERENCE_TIME,
        )


def test_interrupted_activation_recovers_from_provider_read_back(
    tmp_path: Path,
) -> None:
    """Restart follows native truth and retains every account's due work."""
    state = _foundation_state(tmp_path)
    _source, target, codex = tuple(state.accounts)
    record = _activation_record(
        state,
        "4a85762c-e517-4f68-85be-a2ee2e027a66",
    )
    state.journals.begin(record)
    state.journals.advance(
        ProviderId.CLAUDE,
        record.operation_id,
        ActivationPhase.TARGET_ACTIVATED,
        updated_at=REFERENCE_TIME + timedelta(seconds=1),
    )

    restarted = ActivationJournalStore(state.paths.activation_journals)
    interrupted = restarted.load(ProviderId.CLAUDE).active
    assert interrupted is not None
    target_read_back = _selected(
        ProviderId.CLAUDE,
        target.account_id,
        "claude-target-id",
        "claude-target-generation",
        verified_in=2,
    )
    assert (
        decide_activation_recovery(interrupted, target_read_back)
        is ActivationRecoveryAction.COMMIT_VERIFIED
    )
    logged_out = SelectedAccountState(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.LOGGED_OUT,
        account_id=None,
        provider_identity=None,
        runtime_generation=None,
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.LOGGED_OUT,
    )
    assert (
        decide_activation_recovery(interrupted, logged_out)
        is ActivationRecoveryAction.REQUEST_OFFICIAL_ROLLBACK
    )
    unreadable = SelectedAccountState(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.UNREADABLE,
        account_id=None,
        provider_identity=None,
        runtime_generation=None,
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.RECONCILIATION_REQUIRED,
    )
    assert (
        decide_activation_recovery(interrupted, unreadable)
        is ActivationRecoveryAction.RECONCILIATION_REQUIRED
    )
    assert (
        restarted.recover_from_read_back(target_read_back, state.selected)
        is ActivationRecoveryAction.COMMIT_VERIFIED
    )

    recovered = restarted.load(ProviderId.CLAUDE)
    assert recovered.active is None
    assert recovered.history[-1].phase is ActivationPhase.COMMITTED
    assert state.selected.load(ProviderId.CLAUDE) == target_read_back
    codex_selected = state.selected.load(ProviderId.CODEX)
    assert codex_selected is not None
    assert codex_selected.account_id == codex.account_id
    assert (
        OperationQueueStore(state.paths.durable_operations).load()
        == state.queue.load()
    )
