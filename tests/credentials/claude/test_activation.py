"""Load-bearing native Claude activation scenario."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
)
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    ClaudeAuthObservation,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    ProviderAuthState,
    ProviderRuntimeState,
    SelectionCode,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.authority.access_lease import (
    ClaudeSelectedAccessError,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
    ClaudeRemoteControlState,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
)
from sidekick_usages.usage.dashboard.models import DashboardAccount
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.fakes.claude.activation import (
    ClaudeActivationScenario,
    claude_activation_scenario,
)
from tests.fakes.claude.managed import (
    CLAUDE_LOGGED_OUT_STATUS,
    claude_profile_status,
    credential_payload,
    use_synthetic_claude,
)
from tests.fakes.claude.selection import (
    clear_claude_selection,
    execute_selection_worker,
    first_selection_scenario,
    require_worker_observation,
    set_first_recovery_native,
)
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

pytestmark = REQUIRES_MANAGED_RUNTIME

_LOGGED_OUT_CREDENTIAL_PAYLOAD = (
    b'{"claudeAiOauth":{"accessToken":"","refreshToken":""}}'
)


class _ProjectionRecorder:
    """Record only secret-free protected projection evidence."""

    def __init__(self) -> None:
        self.bindings: list[ClaudeStructuredBinding] = []

    def submit(
        self,
        binding: ClaudeStructuredBinding,
        oauth: bytearray,
    ) -> None:
        """Record one secret-free binding."""
        del oauth
        self.bindings.append(binding)


def _execute_activation(
    scenario: ClaudeActivationScenario,
    operation: DueOperation,
) -> WorkerResult:
    with ProviderMutationLock(
        scenario.paths.durable_operations,
        ProviderId.CLAUDE,
        (scenario.source.account_id, scenario.target.account_id),
        timeout_seconds=1.0,
    ).hold() as authority:
        return scenario.executor.execute(operation, authority)


def _selection_lock(
    scenario: ClaudeActivationScenario,
) -> ProviderMutationLock:
    """Lock the exact synthetic Claude source and target accounts."""
    return ProviderMutationLock(
        scenario.paths.durable_operations,
        ProviderId.CLAUDE,
        tuple(
            sorted((scenario.source.account_id, scenario.target.account_id))
        ),
        timeout_seconds=1.0,
    )


def _prevalidate_selection(
    scenario: ClaudeActivationScenario,
    operation: OpenSelectionOperation,
    baseline: FinalizedSelection | None,
) -> PreparedSelection:
    """Exercise executor prevalidation under synthetic provider locks."""
    with _selection_lock(scenario).hold() as authority:
        return scenario.executor.prevalidate_selection(
            operation,
            baseline,
            authority,
        )


def _commit_selection(
    scenario: ClaudeActivationScenario,
    prepared: PreparedSelection,
) -> AuthorityReadyProof:
    """Exercise executor commit under synthetic provider locks."""
    with _selection_lock(scenario).hold() as authority:
        return scenario.executor.commit_selection(prepared, authority)


def _observe_selection(
    scenario: ClaudeActivationScenario,
    operation: OpenSelectionOperation,
) -> SelectedAccountState | None:
    """Exercise neutral executor readback under provider locks."""
    with _selection_lock(scenario).hold() as authority:
        return scenario.executor.readback_selection(operation, authority)


def _rotate_target_authority(scenario: ClaudeActivationScenario) -> None:
    """Rotate the saved target private and secret-free generations."""
    target_authority = scenario.target.authority
    assert isinstance(target_authority, ClaudeAccountAuthority)
    subscription = target_authority.subscription
    assert isinstance(subscription, ClaudeManagedLoginAuthority)
    generation = claude_access_token_generation("sk-ant-oat01-target-rotated")
    rotated = replace(
        scenario.target,
        authority=replace(
            target_authority,
            subscription=replace(subscription, generation=generation),
        ),
    )
    scenario.profiles.write_owned_file(
        scenario.target_profile,
        CLAUDE_CREDENTIAL_FILE,
        credential_payload(
            None,
            None,
            token_suffix="target-rotated",
            access_expires_at=subscription.access_expires_at,
        ),
    )
    PersistenceFilesystem(scenario.paths.accounts).commit_opaque_private(
        encode_version_three(VersionThreeDocument((scenario.source, rotated)))
    )


def _open_selection(
    scenario: ClaudeActivationScenario,
) -> OpenSelectionOperation:
    """Build the coordinator-owned epoch input for one scenario."""
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert baseline is not None
    return OpenSelectionOperation(
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
        started_at=baseline.finalized_at,
        updated_at=baseline.finalized_at,
    )


def _execute_selection(
    scenario: ClaudeActivationScenario,
) -> tuple[PreparedSelection, AuthorityReadyProof]:
    """Run one provider-proven epoch adapter transition."""
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert baseline is not None
    prepared = _prevalidate_selection(
        scenario,
        _open_selection(scenario),
        baseline,
    )
    return prepared, _commit_selection(scenario, prepared)


def _open_proven_generation(
    scenario: ClaudeActivationScenario,
    generation: AuthorityGeneration,
) -> AuthorityGeneration:
    """Open finalized native access after proving saved authority."""
    with _selection_lock(scenario).hold() as authority:
        target = scenario.access.prevalidate(
            scenario.target.account_id, authority
        )
        with scenario.access.open_proven(
            target, generation, authority
        ) as lease:
            return lease.prepared.generation


def _guarded_activation_scenario(
    root: Path,
) -> ClaudeActivationScenario:
    conflict_root = root / "credential-conflict"
    conflict_environment = {
        "ANTHROPIC_API_KEY": "synthetic-parent-secret",
        "HOME": str(conflict_root / "native-home"),
        "PATH": os.defpath,
        "USER": "sidekick-test",
    }
    original_environment = dict(conflict_environment)
    conflict = claude_activation_scenario(
        conflict_root,
        environment=conflict_environment,
    )
    conflict_baseline = conflict.selected.load(ProviderId.CLAUDE)
    assert conflict_baseline is not None
    with pytest.raises(ClaudeActivationError) as rejected:
        _prevalidate_selection(
            conflict,
            _open_selection(conflict),
            conflict_baseline,
        )

    assert (
        rejected.value.failure
        is ClaudeActivationGuardFailure.ANTHROPIC_API_KEY
    )
    assert conflict_environment == original_environment
    assert conflict.runner.calls == []
    assert conflict.script.login_profiles == []
    assert conflict.journals.load(ProviderId.CLAUDE).active is None

    incompatible = claude_activation_scenario(
        root / "remote-control",
        remote_control=ClaudeRemoteControlState.ACTIVE_INCOMPATIBLE,
    )
    native_before = incompatible.native_credentials.read_bytes()
    selected_before = incompatible.selected.load(ProviderId.CLAUDE)
    incompatible_baseline = incompatible.selected.load(ProviderId.CLAUDE)
    assert incompatible_baseline is not None
    with pytest.raises(ClaudeActivationError) as refused:
        _prevalidate_selection(
            incompatible,
            _open_selection(incompatible),
            incompatible_baseline,
        )

    assert (
        refused.value.failure
        is ClaudeActivationGuardFailure.REMOTE_CONTROL_INCOMPATIBLE
    )
    assert incompatible.native_credentials.read_bytes() == native_before
    assert incompatible.selected.load(ProviderId.CLAUDE) == selected_before
    assert incompatible.script.login_profiles == []
    assert incompatible.journals.load(ProviderId.CLAUDE).active is None

    approved = claude_activation_scenario(
        root / "activation",
        target_setup_token=True,
    )
    assert isinstance(approved.target.authority, ClaudeAccountAuthority)
    assert approved.target.authority.setup_token is not None
    return approved


def test_native_activation_retains_source_and_commits_verified_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mixed target uses one approved, provider-proven native switch."""
    use_synthetic_claude(monkeypatch)
    scenario = _guarded_activation_scenario(tmp_path)
    prepared, proof = _execute_selection(scenario)
    readback = _observe_selection(scenario, _open_selection(scenario))
    assert readback is not None

    assert (
        proof.provider_id,
        proof.account_id,
        proof.generation != prepared.target_generation,
        _open_proven_generation(scenario, proof.generation),
        proof.epoch,
        proof.safe_code,
        readback.runtime_state,
        readback.account_id,
        readback.runtime_generation,
    ) == (
        ProviderId.CLAUDE,
        scenario.target.account_id,
        True,
        proof.generation,
        prepared.pending_epoch,
        SelectionCode.SELECTION_SUCCEEDED,
        ProviderRuntimeState.SAVED_ACTIVE,
        scenario.target.account_id,
        proof.generation,
    )
    current_source = scenario.store.read_saved(scenario.source.account_id)
    assert current_source is not None
    current_source_authority = current_source.authority
    assert isinstance(current_source_authority, ClaudeAccountAuthority)
    current_source_subscription = current_source_authority.subscription
    assert isinstance(current_source_subscription, ClaudeManagedLoginAuthority)
    assert current_source_subscription.generation == (
        claude_access_token_generation("sk-ant-oat01-source-retained")
    )
    retained = scenario.profiles.read_owned_file(
        scenario.source_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    unchanged_target = scenario.profiles.read_owned_file(
        scenario.target_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    assert (
        None if retained is None else retained.data,
        None if unchanged_target is None else unchanged_target.data,
        scenario.native_credentials.read_bytes(),
    ) == (
        scenario.retained_source_payload,
        scenario.target_payload,
        scenario.native_target_payload,
    )
    login_environments = [
        environment
        for (_executable, arguments), environment in zip(
            scenario.runner.calls,
            scenario.runner.environments,
            strict=True,
        )
        if arguments == ("auth", "login", "--claudeai")
    ]
    assert (
        scenario.script.login_profiles,
        tuple(
            (
                None
                if environment is None
                else environment.get(CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY)
            )
            for environment in login_environments
        ),
    ) == (
        [
            scenario.source_profile,
            scenario.native.config_directory,
        ],
        (str(scenario.source_profile), None),
    )
    claude_state = scenario.selected.load(ProviderId.CLAUDE)
    assert claude_state is not None
    assert claude_state.account_id == scenario.source.account_id
    runtime_auth = RuntimeAuthObservationStore(
        scenario.paths.durable_operations
    ).load_native(ProviderId.CLAUDE)
    assert runtime_auth is not None
    assert (
        runtime_auth.state,
        runtime_auth.provider_identity,
        runtime_auth.generation,
    ) == (
        ProviderAuthState.ACTIVE,
        scenario.target.provider_identity,
        claude_access_token_generation("sk-ant-oat01-target-native"),
    )
    assert scenario.selected.load(ProviderId.CODEX) == scenario.codex_state
    journal = scenario.journals.load(ProviderId.CLAUDE)
    assert (journal.active, len(journal.history)) == (None, 1)
    committed = journal.history[0]
    assert (
        committed.phase,
        committed.target_account_id,
        committed.verified_runtime_generation,
    ) == (
        ActivationPhase.COMMITTED,
        scenario.target.account_id,
        runtime_auth.generation,
    )
    assert isinstance(committed.native_auth_baseline, ClaudeAuthObservation)
    assert committed.native_auth_baseline.modified_milliseconds is not None

    status_only = claude_activation_scenario(
        tmp_path / "status-only",
        status_only_native_login=True,
    )
    status_only_selected = status_only.selected.load(ProviderId.CLAUDE)
    status_only_result = _execute_activation(
        status_only,
        status_only.operation,
    )
    status_only_runtime = RuntimeAuthObservationStore(
        status_only.paths.durable_operations
    ).load_native(ProviderId.CLAUDE)
    status_only_dashboard = CachedDashboardService(status_only.paths).load(
        status_only.codex_state.finalized_at
    )
    status_only_claude = status_only_dashboard.providers[0]

    assert (
        status_only_result.outcome,
        status_only_result.failure_code,
        status_only.selected.load(ProviderId.CLAUDE),
        status_only.native_credentials.read_bytes()
        == status_only.native_target_payload,
        (
            None
            if status_only_runtime is None
            else (
                status_only_runtime.state,
                status_only_runtime.provider_identity,
            )
        ),
        status_only_claude.runtime_state,
        status_only_claude.active_account_id,
        any(
            isinstance(row, DashboardAccount) and row.active
            for row in status_only_claude.rows
        ),
    ) == (
        WorkerOutcome.ACTION_REQUIRED,
        ClaudeActivationFailure.RECONCILIATION_REQUIRED.failure_code,
        status_only_selected,
        False,
        (
            ProviderAuthState.ACTIVE,
            status_only.target.provider_identity,
        ),
        ProviderRuntimeState.EXTERNAL_ACTIVE,
        None,
        False,
    )
    status_only_journal = status_only.journals.load(ProviderId.CLAUDE)
    status_only_active = status_only_journal.active
    assert status_only_active is not None
    assert (
        status_only_active.phase,
        status_only_active.reconciliation_origin_phase,
    ) == (
        ActivationPhase.RECONCILIATION_REQUIRED,
        ActivationPhase.OUTGOING_RETAINED,
    )

    unpropagated = claude_activation_scenario(
        tmp_path / "unpropagated",
        advance_native_mtime=False,
    )
    unpropagated_selected = unpropagated.selected.load(ProviderId.CLAUDE)
    unpropagated_result = _execute_activation(
        unpropagated,
        unpropagated.operation,
    )
    unpropagated_runtime = RuntimeAuthObservationStore(
        unpropagated.paths.durable_operations
    ).load_native(ProviderId.CLAUDE)

    assert (
        unpropagated_result.outcome,
        unpropagated_result.failure_code,
        unpropagated.selected.load(ProviderId.CLAUDE),
        unpropagated.native_credentials.read_bytes(),
        (
            None
            if unpropagated_runtime is None
            else unpropagated_runtime.provider_identity
        ),
    ) == (
        WorkerOutcome.ACTION_REQUIRED,
        ClaudeActivationFailure.RECONCILIATION_REQUIRED.failure_code,
        unpropagated_selected,
        unpropagated.native_target_payload,
        unpropagated.target.provider_identity,
    )


def test_selection_repairs_official_logged_out_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved target replaces exact provider-owned logged-out state."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(
        tmp_path,
        native_logged_out=True,
    )
    scenario.script.set_authority(
        scenario.native.config_directory,
        _LOGGED_OUT_CREDENTIAL_PAYLOAD,
        CLAUDE_LOGGED_OUT_STATUS,
    )

    _, proof = _execute_selection(scenario)
    record = scenario.journals.load(ProviderId.CLAUDE).history[-1]

    assert (
        proof.account_id,
        scenario.native_credentials.read_bytes(),
        record.native_auth_baseline.state,
        scenario.script.login_profiles,
    ) == (
        scenario.target.account_id,
        scenario.native_target_payload,
        ProviderAuthState.LOGGED_OUT,
        [scenario.native.config_directory],
    )


def test_selection_commit_refuses_rotated_prevalidated_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target generation change cannot authorize native mutation."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert baseline is not None
    prepared = _prevalidate_selection(
        scenario,
        _open_selection(scenario),
        baseline,
    )
    _rotate_target_authority(scenario)
    native_before = scenario.native_credentials.read_bytes()
    selected_before = scenario.selected.load(ProviderId.CLAUDE)

    with pytest.raises(ClaudeSelectedAccessError) as rejected:
        _commit_selection(scenario, prepared)

    assert rejected.value.code is SelectionCode.AUTHORITY_PROOF_FAILED
    assert scenario.native_credentials.read_bytes() == native_before
    assert scenario.selected.load(ProviderId.CLAUDE) == selected_before
    assert scenario.script.login_profiles == []
    assert scenario.journals.load(ProviderId.CLAUDE).active is None


def test_selection_prevalidation_does_not_create_missing_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing managed profile stays absent during read-only proof."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert baseline is not None
    scenario.profiles.destroy_owned_directory(scenario.target_profile)

    with pytest.raises(ClaudeActivationError) as rejected:
        _prevalidate_selection(
            scenario,
            _open_selection(scenario),
            baseline,
        )

    assert rejected.value.failure is ClaudeActivationFailure.TARGET_UNAVAILABLE
    assert not os.path.lexists(scenario.target_profile)
    assert scenario.script.login_profiles == []
    assert scenario.journals.load(ProviderId.CLAUDE).active is None


@pytest.mark.parametrize(
    ("native_start", "recovery_state", "expected"),
    [
        ("target", None, "target"),
        ("logged_out", None, "target"),
        ("unrelated", None, "refused"),
        ("logged_out", "target", "target"),
        ("logged_out", "logged_out", "logged_out"),
        ("logged_out", "unrelated", "reconciliation"),
    ],
)
def test_first_selection_lifecycle_uses_no_manufactured_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_start: str,
    recovery_state: str | None,
    expected: str,
) -> None:
    """First selection commits or recovers only from exact native truth."""
    use_synthetic_claude(monkeypatch)
    scenario = first_selection_scenario(
        tmp_path,
        native_start,
        recovery_state,
    )
    operation = replace(
        _open_selection(scenario),
        baseline_account_id=None,
    )
    clear_claude_selection(scenario)

    prevalidate_due = replace(
        scenario.operation,
        kind=OperationKind.SELECTION_PREVALIDATE,
        selection_operation_id=operation.operation_id,
    )
    prevalidated = execute_selection_worker(
        scenario,
        prevalidate_due,
        operation,
        None,
    )
    if expected == "refused":
        assert (
            prevalidated.outcome,
            prevalidated.failure_code,
            prevalidated.selection,
        ) == (
            WorkerOutcome.ACTION_REQUIRED,
            SelectionCode.UNCOORDINATED_AUTH_MUTATION.value,
            None,
        )
        assert scenario.script.login_profiles == []
        assert scenario.journals.load(ProviderId.CLAUDE).active is None
        return

    prepared_generation = require_worker_observation(
        prevalidated,
        operation,
        OperationKind.SELECTION_PREVALIDATE,
        scenario.target.account_id,
    )
    assert prepared_generation is not None
    prepared_operation = replace(
        operation,
        prepared_generation=prepared_generation,
        phase=SelectionPhase.COMMITTING,
    )
    commit_due = replace(
        scenario.operation,
        kind=OperationKind.SELECTION_COMMIT,
        selection_operation_id=operation.operation_id,
    )
    if recovery_state is None:
        committed = execute_selection_worker(
            scenario,
            commit_due,
            prepared_operation,
            None,
        )
        committed_generation = require_worker_observation(
            committed,
            operation,
            OperationKind.SELECTION_COMMIT,
            scenario.target.account_id,
        )
        assert committed_generation is not None
    else:
        with pytest.raises(KeyboardInterrupt):
            execute_selection_worker(
                scenario,
                commit_due,
                prepared_operation,
                None,
            )
        assert recovery_state is not None
        set_first_recovery_native(scenario, recovery_state)
        recovery = replace(
            scenario.operation,
            kind=OperationKind.RECONCILE,
        )
        result = _execute_activation(scenario, recovery)
        assert result.outcome is (
            WorkerOutcome.ACTION_REQUIRED
            if expected == "reconciliation"
            else WorkerOutcome.SUCCEEDED
        )

    journal = scenario.journals.load(ProviderId.CLAUDE)
    record = journal.active or (
        journal.history[-1] if journal.history else None
    )
    if native_start == "target":
        assert record is None
    else:
        assert record is not None
        assert record.selected_baseline is None
        assert (
            record.native_auth_baseline.state is ProviderAuthState.LOGGED_OUT
        )

    observed = _observe_selection(scenario, operation)
    assert observed is not None
    readback = execute_selection_worker(
        scenario,
        replace(
            scenario.operation,
            kind=OperationKind.SELECTION_READBACK,
            selection_operation_id=operation.operation_id,
        ),
        prepared_operation,
        None,
    )
    require_worker_observation(
        readback,
        operation,
        OperationKind.SELECTION_READBACK,
        scenario.target.account_id if expected == "target" else None,
    )
    assert scenario.selected.load(ProviderId.CLAUDE) is None
    assert scenario.source_profile not in scenario.script.login_profiles
    if expected == "target":
        assert (
            observed.runtime_state,
            observed.account_id,
            None if record is None else record.phase,
        ) == (
            ProviderRuntimeState.SAVED_ACTIVE,
            scenario.target.account_id,
            None if native_start == "target" else ActivationPhase.COMMITTED,
        )
    elif expected == "logged_out":
        assert record is not None
        assert (
            observed.runtime_state,
            record.phase,
            record.outcome,
        ) == (
            ProviderRuntimeState.LOGGED_OUT,
            ActivationPhase.ROLLED_BACK,
            ActivationOutcome.LOGGED_OUT,
        )
    else:
        assert record is not None
        assert (
            observed.runtime_state,
            record.phase,
            record.failure_code,
        ) == (
            ProviderRuntimeState.EXTERNAL_ACTIVE,
            ActivationPhase.RECONCILIATION_REQUIRED,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED.failure_code,
        )


def test_same_account_generation_rollover_projects_without_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project a stably refreshed native authority without logging in."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)
    baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert baseline is not None
    source_status, _identity = claude_profile_status("source")
    scenario.script.set_authority(
        scenario.native.config_directory,
        scenario.retained_source_payload,
        source_status,
    )
    native_before = scenario.native_credentials.read_bytes()
    projection = _ProjectionRecorder()
    monkeypatch.setattr(scenario.executor, "_projection", projection)
    active = replace(
        _open_selection(scenario),
        baseline_account_id=scenario.source.account_id,
        target_account_id=scenario.source.account_id,
    )
    prevalidate_due = replace(
        scenario.operation,
        account_id=scenario.source.account_id,
        kind=OperationKind.SELECTION_PREVALIDATE,
        selection_operation_id=active.operation_id,
    )
    commit_due = replace(
        prevalidate_due,
        kind=OperationKind.SELECTION_COMMIT,
    )
    prevalidated = execute_selection_worker(
        scenario, prevalidate_due, active, baseline
    )
    generation = require_worker_observation(
        prevalidated,
        active,
        OperationKind.SELECTION_PREVALIDATE,
        scenario.source.account_id,
    )
    assert generation is not None
    prepared = replace(
        active,
        prepared_generation=generation,
        phase=SelectionPhase.COMMITTING,
    )
    committed = execute_selection_worker(
        scenario, commit_due, prepared, baseline
    )
    assert scenario.native_credentials.read_bytes() == native_before
    assert scenario.store.read_saved(scenario.source.account_id) == (
        scenario.source
    )
    assert scenario.journals.load(ProviderId.CLAUDE).active is None
    assert not scenario.journals.load(ProviderId.CLAUDE).history
    assert scenario.script.login_profiles == []
    scenario.script.set_authority(
        scenario.native.config_directory,
        scenario.native_target_payload,
        source_status,
    )
    readback = execute_selection_worker(
        scenario,
        replace(prevalidate_due, kind=OperationKind.SELECTION_READBACK),
        prepared,
        baseline,
    )

    assert generation != baseline.generation
    assert (
        require_worker_observation(
            committed,
            active,
            OperationKind.SELECTION_COMMIT,
            scenario.source.account_id,
        )
        == generation
    )
    assert require_worker_observation(
        readback,
        active,
        OperationKind.SELECTION_READBACK,
        scenario.source.account_id,
    ) == claude_access_token_generation("sk-ant-oat01-target-native")
    assert projection.bindings == [
        ClaudeStructuredBinding(
            operation_id=active.operation_id,
            account_id=scenario.source.account_id,
            generation=generation,
            epoch=active.pending_epoch,
        )
    ]


def test_selection_readback_observes_baseline_target_or_unrelated_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readback reports native truth without assigning a pending epoch."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)
    operation = _open_selection(scenario)
    selected_before = scenario.selected.load(ProviderId.CLAUDE)
    assert selected_before is not None
    journals_before = scenario.journals.load(ProviderId.CLAUDE)
    observations = RuntimeAuthObservationStore(
        scenario.paths.durable_operations
    )
    observation_before = observations.load_native(ProviderId.CLAUDE)

    baseline = _observe_selection(scenario, operation)
    assert scenario.selected.load(ProviderId.CLAUDE) == selected_before
    assert scenario.journals.load(ProviderId.CLAUDE) == journals_before
    assert observations.load_native(ProviderId.CLAUDE) == observation_before
    prepared = _prevalidate_selection(
        scenario,
        operation,
        selected_before,
    )
    proof = _commit_selection(scenario, prepared)
    selected_after_commit = scenario.selected.load(ProviderId.CLAUDE)
    journals_after_commit = scenario.journals.load(ProviderId.CLAUDE)
    observation_after_commit = observations.load_native(ProviderId.CLAUDE)
    target = _observe_selection(scenario, operation)
    assert scenario.selected.load(ProviderId.CLAUDE) == selected_after_commit
    assert scenario.journals.load(ProviderId.CLAUDE) == journals_after_commit
    assert (
        observations.load_native(ProviderId.CLAUDE) == observation_after_commit
    )

    external_status, _ = claude_profile_status("external")
    target_authority = scenario.target.authority
    assert isinstance(target_authority, ClaudeAccountAuthority)
    target_subscription = target_authority.subscription
    assert isinstance(target_subscription, ClaudeManagedLoginAuthority)
    external_payload = credential_payload(
        None,
        None,
        token_suffix="external-native",
        access_expires_at=target_subscription.access_expires_at,
    )
    scenario.script.set_authority(
        scenario.native.config_directory,
        external_payload,
        external_status,
    )
    unrelated = _observe_selection(scenario, operation)
    assert scenario.selected.load(ProviderId.CLAUDE) == selected_after_commit
    assert scenario.journals.load(ProviderId.CLAUDE) == journals_after_commit
    assert (
        observations.load_native(ProviderId.CLAUDE) == observation_after_commit
    )

    assert baseline is not None
    assert target is not None
    assert unrelated is not None
    assert (
        baseline.runtime_state,
        baseline.account_id,
        baseline.runtime_generation,
        target.runtime_state,
        target.account_id,
        target.runtime_generation,
        unrelated.runtime_state,
        unrelated.account_id,
    ) == (
        ProviderRuntimeState.SAVED_ACTIVE,
        scenario.source.account_id,
        selected_before.generation,
        ProviderRuntimeState.SAVED_ACTIVE,
        scenario.target.account_id,
        proof.generation,
        ProviderRuntimeState.EXTERNAL_ACTIVE,
        None,
    )
