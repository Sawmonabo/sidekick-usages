"""Synthetic boundaries for Claude global-selection worker phases."""

from pathlib import Path

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
)
from sidekick_usages.core.selection.types import OperationKind, SelectionPhase
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.selection import SelectedStateDocument
from sidekick_usages.persistence.schema.selection import encode_selected_state
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from tests.fakes.claude.activation import (
    ClaudeActivationScenario,
    claude_activation_scenario,
)
from tests.fakes.claude.managed import (
    claude_profile_status,
    credential_payload,
)
from tests.support.time import REFERENCE_TIME


def existing_selection_operation(
    scenario: ClaudeActivationScenario,
) -> tuple[OpenSelectionOperation, FinalizedSelection]:
    """Build one open selection from the synthetic finalized baseline."""
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    if baseline is None:
        raise AssertionError("Synthetic Claude baseline is unavailable.")
    return (
        OpenSelectionOperation(
            operation_id=scenario.operation.operation_id,
            provider_id=ProviderId.CLAUDE,
            baseline_account_id=baseline.account_id,
            target_account_id=scenario.target.account_id,
            prepared_generation=None,
            target_generation=None,
            baseline_epoch=baseline.epoch,
            pending_epoch=baseline.epoch.next(),
            phase=SelectionPhase.PREVALIDATING,
            required_participant_ids=(),
            ready_participant_ids=(),
            lost_after_commit_participant_ids=(),
            confirmed_dead_before_commit_count=0,
            confirmed_dead_before_commit_code=None,
            outcome_code=None,
            started_at=REFERENCE_TIME,
            updated_at=REFERENCE_TIME,
        ),
        baseline,
    )


def first_selection_scenario(
    root: Path,
    native_start: str,
    recovery_state: str | None,
) -> ClaudeActivationScenario:
    """Build one explicitly unselected native Claude worker scenario."""
    interruption = (
        None
        if recovery_state is None
        else KeyboardInterrupt("synthetic first-selection interruption")
    )
    scenario = claude_activation_scenario(
        root,
        native_logged_out=native_start == "logged_out",
        interrupt_after_native_login=interruption,
    )
    if native_start == "target":
        target_status, _ = claude_profile_status("target")
        scenario.script.set_authority(
            scenario.native.config_directory,
            scenario.native_target_payload,
            target_status,
        )
    return scenario


def clear_claude_selection(scenario: ClaudeActivationScenario) -> None:
    """Leave only the unrelated synthetic Codex finalized selection."""
    PersistenceFilesystem(scenario.paths.selected_state).commit_opaque_private(
        encode_selected_state(SelectedStateDocument((scenario.codex_state,)))
    )
    if scenario.selected.load(ProviderId.CLAUDE) is not None:
        raise AssertionError("Synthetic Claude selection was not cleared.")


def set_first_recovery_native(
    scenario: ClaudeActivationScenario,
    recovery_state: str,
) -> None:
    """Set exact post-interruption native truth for recovery."""
    if recovery_state == "logged_out":
        scenario.native_credentials.unlink()
        return
    if recovery_state != "unrelated":
        return
    external_status, _ = claude_profile_status("external")
    target_authority = scenario.target.authority
    if not isinstance(target_authority, ClaudeAccountAuthority):
        raise AssertionError("Synthetic target authority is not Claude.")
    target_subscription = target_authority.subscription
    if not isinstance(target_subscription, ClaudeManagedLoginAuthority):
        raise AssertionError("Synthetic target login is not managed.")
    scenario.script.set_authority(
        scenario.native.config_directory,
        credential_payload(
            None,
            None,
            token_suffix="external-first-selection",
            access_expires_at=target_subscription.access_expires_at,
        ),
        external_status,
    )


def execute_selection_worker(
    scenario: ClaudeActivationScenario,
    due: DueOperation,
    active: OpenSelectionOperation,
    baseline: FinalizedSelection | None,
) -> WorkerResult:
    """Exercise one isolated selection phase under exact account locks."""
    account_ids = (
        (active.target_account_id,)
        if active.baseline_account_id is None
        else tuple(
            sorted(
                {
                    active.baseline_account_id,
                    active.target_account_id,
                }
            )
        )
    )
    with ProviderMutationLock(
        scenario.paths.durable_operations,
        ProviderId.CLAUDE,
        account_ids,
        timeout_seconds=1.0,
    ).hold() as authority:
        return scenario.executor.execute_selection(
            due,
            active,
            baseline,
            authority,
        )


def require_worker_observation(
    result: WorkerResult,
    operation: OpenSelectionOperation,
    kind: OperationKind,
    account_id: SidekickAccountId | None,
) -> AuthorityGeneration | None:
    """Require one correlated secret-free selection worker observation."""
    selection = result.selection
    if selection is None:
        raise AssertionError("Selection worker result has no observation.")
    actual = (
        result.outcome,
        result.failure_code,
        selection.operation_id,
        selection.provider_id,
        selection.kind,
        selection.pending_epoch,
        selection.observed_account_id,
    )
    expected = (
        WorkerOutcome.SUCCEEDED,
        None,
        operation.operation_id,
        ProviderId.CLAUDE,
        kind,
        operation.pending_epoch,
        account_id,
    )
    if actual != expected:
        raise AssertionError("Selection worker observation is uncorrelated.")
    return selection.observed_generation
