"""Durable selection and operation-queue state tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from tests.fakes.daemon.foundation import (
    foundation_state,
    operation,
    selected,
)
from tests.support.time import REFERENCE_TIME

PENDING_SWITCH_OPERATION_ID = OperationId(
    "bb413f38-2b11-418a-a4a7-b0e45666067e"
)
APPROVED_SWITCH_OPERATION_ID = OperationId(
    "e16508f9-aea0-4c51-9d16-1b4168b3411a"
)


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
            "claude-target-id",
            "claude-target-generation",
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
        allow_remote_control_disconnect=True,
    )
    coalesced = state.queue.enqueue(approved_switch)
    assert coalesced.operation_id == pending_switch.operation_id
    assert coalesced.allow_remote_control_disconnect
    assert state.queue.find(coalesced.operation_id) == coalesced
    with pytest.raises(
        ValueError,
        match="only valid for Claude activation",
    ):
        replace(
            approved_switch,
            provider_id=ProviderId.CODEX,
        )
