"""Durable selection and operation-queue state tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.entrypoints import worker
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.schema.selection import decode_operation_queue
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from tests.fakes.credentials.refresh import login_account
from tests.fakes.daemon.foundation import (
    foundation_state,
    operation,
    selected,
)
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
)
from tests.support.time import REFERENCE_TIME

PENDING_SWITCH_OPERATION_ID = OperationId(
    "bb413f38-2b11-418a-a4a7-b0e45666067e"
)
APPROVED_SWITCH_OPERATION_ID = OperationId(
    "e16508f9-aea0-4c51-9d16-1b4168b3411a"
)
MANAGED_AUTH_MIGRATION_REQUIRED_CODE = "managed_auth_migration_required"


def test_unmanaged_workers_require_migration_before_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove legacy authorities stop before any provider composition."""
    root = tmp_path / "unmanaged-workers"
    store = make_account_store(
        root,
        (
            Account(
                label=AccountLabel("claude-setup"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-claude-setup-secret"
                ),
            ),
            login_account("claude-legacy"),
            Account(
                label=AccountLabel("codex-legacy"),
                credentials=CodexCredentials(
                    access_token="test-only-codex-secret",
                    account_id="test-only-codex-account",
                ),
            ),
        ),
    )
    paths = make_application_paths(root)
    queue = OperationQueueStore(paths.durable_operations)
    operations = tuple(
        operation(
            account.account_id,
            account.provider_id,
            str(new_operation_id()),
        )
        for account in store.saved_accounts()
    )
    for due_operation in operations:
        queue.enqueue(due_operation)
        queue.transition(
            due_operation.operation_id,
            OperationState.RUNNING,
            updated_at=due_operation.updated_at,
        )

    def reject_provider_composition(
        *arguments: object,
        **keywords: object,
    ) -> None:
        del arguments, keywords
        raise AssertionError(
            "Providers must not be composed before migration."
        )

    monkeypatch.setattr(worker, "discover_application_paths", lambda: paths)
    monkeypatch.setattr(
        worker,
        "ClaudeProfileCapabilityFactory",
        reject_provider_composition,
    )
    monkeypatch.setattr(
        worker,
        "compose_codex_managed_authority",
        reject_provider_composition,
    )
    results = WorkerResultStore(paths.durable_operations)
    outcomes: list[tuple[WorkerOutcome, str | None]] = []
    for due_operation in operations:
        assert worker.main((str(due_operation.operation_id),)) == 0
        result = results.load(due_operation.operation_id)
        assert result is not None
        outcomes.append((result.outcome, result.failure_code))

    migration_required = (
        WorkerOutcome.ACTION_REQUIRED,
        MANAGED_AUTH_MIGRATION_REQUIRED_CODE,
    )
    assert outcomes == [
        (WorkerOutcome.SUCCEEDED, None),
        migration_required,
        migration_required,
    ]
    assert not paths.private_claude_profiles.exists()
    assert not paths.private_codex_profiles.exists()


def test_selection_and_queue_preserve_stable_independent_state(
    tmp_path: Path,
) -> None:
    """One provider selection changes without label or queue coupling."""
    state = foundation_state(tmp_path)
    _source, target, _codex = tuple(state.accounts)
    duplicate = operation(
        target.account_id,
        ProviderId.CLAUDE,
        "14c50df9-c216-4f99-a88f-4e1a3ab8eb5b",
        due_in=5,
    )
    assert (
        state.queue.enqueue(duplicate).operation_id
        == state.operations[1].operation_id
    )
    current = state.selected.load(ProviderId.CLAUDE)
    assert current is not None
    state.selected.compare_and_swap(
        selected(
            ProviderId.CLAUDE,
            target.account_id,
            "claude-target-generation",
            epoch=1,
            verified_in=3,
        ),
        expected=current,
    )

    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert state.selected.load(ProviderId.CODEX) == state.codex_state
    codex_operation = state.operations[2]
    running = state.queue.transition(
        codex_operation.operation_id,
        OperationState.RUNNING,
        updated_at=REFERENCE_TIME,
    )
    state.queue.transition(
        running.operation_id,
        OperationState.ACTION_REQUIRED,
        updated_at=REFERENCE_TIME,
        failure_code="login_required",
    )
    redue = state.queue.enqueue(
        operation(
            codex_operation.required_account_id,
            ProviderId.CODEX,
            "d101095e-7bda-43ad-b55d-b8ecb5a7ec66",
        )
    )
    assert redue.state is OperationState.SCHEDULED
    assert redue.due_at == REFERENCE_TIME
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
        state.queue.get(
            target.provider_id,
            target.account_id,
            OperationKind.MAINTAIN,
        )
        is not None
    )
    pending_switch = DueOperation(
        operation_id=PENDING_SWITCH_OPERATION_ID,
        provider_id=ProviderId.CLAUDE,
        account_id=target.account_id,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    state.queue.enqueue(pending_switch)
    approved_switch = replace(
        pending_switch,
        operation_id=APPROVED_SWITCH_OPERATION_ID,
    )
    coalesced = state.queue.enqueue(approved_switch)
    assert coalesced.operation_id == pending_switch.operation_id
    assert state.queue.find(coalesced.operation_id) == coalesced
    payload = state.queue.path.read_bytes()
    legacy_field = b'"allow_remote_control_disconnect":false,'
    assert b"allow_remote_control_disconnect" not in payload
    with pytest.raises(InvalidSchemaError):
        decode_operation_queue(
            payload.replace(b'"attempts":', legacy_field + b'"attempts":', 1)
        )
    phase_id = new_operation_id()
    phase = replace(
        pending_switch,
        operation_id=phase_id,
        kind=OperationKind.SELECTION_READBACK,
        selection_operation_id=phase_id,
    )
    state.queue.enqueue(phase)
    previous = (
        state.queue.path.read_bytes()
        .replace(
            b'"schema_version":5',
            b'"schema_version":4',
        )
        .replace(
            b'"selection_operation_id":null,',
            b"",
        )
        .replace(
            f'"selection_operation_id":"{phase_id}",'.encode(),
            b"",
        )
    )
    restored = next(
        operation
        for operation in decode_operation_queue(previous).operations
        if operation.kind is OperationKind.SELECTION_READBACK
    )
    assert restored.required_selection_operation_id == phase_id
