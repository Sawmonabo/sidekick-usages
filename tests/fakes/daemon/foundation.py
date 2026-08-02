"""Shared durable state graph for daemon foundation tests."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from tests.support.accounts import saved_account
from tests.support.persistence import (
    make_application_paths,
    seed_finalized_selections,
)
from tests.support.time import REFERENCE_TIME

CLAUDE_NATIVE_OPERATION_ID = OperationId(
    "ddd13f38-2b11-418a-a4a7-b0e45666067e"
)


def accounts() -> AccountIndex:
    """Build the provider-independent saved-account fixture."""
    stored_accounts = (
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
    return AccountIndex(stored_accounts)


def selected(
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    generation: str,
    *,
    epoch: int = 0,
    verified_in: int = 0,
) -> FinalizedSelection:
    """Build a finalized provider selection."""
    return FinalizedSelection(
        provider_id=provider_id,
        account_id=account_id,
        epoch=SelectionEpoch(epoch),
        generation=AuthorityGeneration(generation),
        finalized_at=REFERENCE_TIME + timedelta(seconds=verified_in),
    )


def operation(
    account: SidekickAccountId,
    provider_id: ProviderId,
    operation_id: str,
    *,
    due_in: int = 0,
) -> DueOperation:
    """Build one scheduled maintenance operation."""
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
class FoundationState:
    """Compact synthetic state graph shared by daemon scenarios."""

    paths: ApplicationPaths
    accounts: AccountIndex
    selected: SelectedStateStore
    journals: ActivationJournalStore
    queue: OperationQueueStore
    operations: tuple[DueOperation, ...]
    codex_state: FinalizedSelection


def foundation_state(tmp_path: Path) -> FoundationState:
    """Persist one deterministic cross-provider daemon state graph."""
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.selected_state).repair_parent_permissions()
    saved_accounts = accounts()
    source, target, codex = tuple(saved_accounts)
    selected_store = SelectedStateStore(paths.selected_state)
    claude_state = selected(
        ProviderId.CLAUDE,
        source.account_id,
        "claude-source-generation",
    )
    codex_state = selected(
        ProviderId.CODEX,
        codex.account_id,
        "codex-generation",
    )
    seed_finalized_selections(paths, claude_state, codex_state)
    operations = (
        operation(
            source.account_id,
            ProviderId.CLAUDE,
            "806fd66f-591b-4341-b31e-3d25405faf52",
        ),
        operation(
            target.account_id,
            ProviderId.CLAUDE,
            "cf39e3c5-2517-4c79-937a-4f7d1fe5c916",
        ),
        operation(
            codex.account_id,
            ProviderId.CODEX,
            "9630cd63-b9c3-4a24-8c78-b8ba4876411b",
        ),
    )
    queue = OperationQueueStore(paths.durable_operations)
    for due_operation in operations:
        queue.enqueue(due_operation)
    return FoundationState(
        paths=paths,
        accounts=saved_accounts,
        selected=selected_store,
        journals=ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        ),
        queue=queue,
        operations=operations,
        codex_state=codex_state,
    )
