"""Load-bearing Claude activation recovery scenarios."""

import os
from datetime import timedelta
from itertools import cycle
from pathlib import Path

import pytest

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import ProviderFileSnapshot
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import ClaudeCommandResult
from sidekick_usages.usage.dashboard.models import DashboardAccount
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.fakes.claude.activation import (
    ClaudeRecoveryScenario,
    claude_recovery_scenario,
)
from tests.fakes.claude.managed import (
    ClaudeRunner,
    claude_auth_status_payload,
    claude_capabilities,
    credential_payload,
    use_synthetic_claude,
)
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME

pytestmark = REQUIRES_MANAGED_RUNTIME
_EXPECTED_NATIVE_LOGINS = 2
_STATUS_ONLY_TOKEN_SUFFIX = "status-only-native"
_KNOWN_STATUS = claude_auth_status_payload(
    "known@example.test",
    "provider-organization-known",
)
_UNKNOWN_STATUS = claude_auth_status_payload(
    "unknown@example.test",
    "provider-organization-unknown",
)
_STATUS_ONLY_STATUS = claude_auth_status_payload(
    "status-only@example.test",
    "provider-organization-status-only",
)
_INCOMPLETE_STATUS = (
    b'{"loggedIn":true,"authMethod":"claude.ai",'
    b'"apiProvider":"firstParty","email":"incomplete@example.test"}'
)


class _SimulatedCrash(BaseException):
    """Stop activation after the native provider mutation."""


def _interrupt(scenario: ClaudeRecoveryScenario) -> None:
    """Run one activation through its simulated native-write crash."""
    with (
        ProviderMutationLock(
            scenario.paths.durable_operations,
            ProviderId.CLAUDE,
            (
                scenario.source.account_id,
                scenario.target.account_id,
            ),
            timeout_seconds=1.0,
        ).hold() as authority,
        pytest.raises(_SimulatedCrash),
    ):
        scenario.executor.execute(scenario.activation, authority)


def _recover(
    scenario: ClaudeRecoveryScenario,
    operation: DueOperation,
) -> WorkerResult:
    """Run one recovery under the complete Claude account authority."""
    with ProviderMutationLock(
        scenario.paths.durable_operations,
        ProviderId.CLAUDE,
        scenario.account_ids,
        timeout_seconds=1.0,
    ).hold() as authority:
        return scenario.executor.execute(operation, authority)


def _assert_raced_native_reconciliation(
    scenario: ClaudeRecoveryScenario,
    observations: RuntimeAuthObservationStore,
    selected_baseline: FinalizedSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove a raced read converges and relation failure stays external."""
    original_read = PersistenceFilesystem.read_provider_owned
    native_reads = 0

    def change_native_after_first_read(
        filesystem: PersistenceFilesystem,
        limit: int,
    ) -> ProviderFileSnapshot | None:
        nonlocal native_reads
        snapshot = original_read(filesystem, limit)
        if (
            filesystem.authority_path == scenario.native_credentials
            and native_reads == 0
        ):
            native_reads += 1
            scenario.script.set_authority(
                scenario.native.config_directory,
                scenario.known_native_payload,
                _KNOWN_STATUS,
            )
        return snapshot

    monkeypatch.setattr(
        PersistenceFilesystem,
        "read_provider_owned",
        change_native_after_first_read,
    )
    raced = _recover(scenario, scenario.native_reconciliation)
    selected_after_race = scenario.selected.load(ProviderId.CLAUDE)
    assert raced.outcome is WorkerOutcome.NO_CHANGE
    assert native_reads == 1
    assert selected_after_race == selected_baseline

    converged = _recover(scenario, scenario.native_reconciliation)
    settled = _recover(scenario, scenario.native_reconciliation)
    selected_after_convergence = scenario.selected.load(ProviderId.CLAUDE)
    assert converged.outcome is WorkerOutcome.SUCCEEDED
    assert settled.outcome is WorkerOutcome.SUCCEEDED
    assert converged.related_runtime_authority is not None
    assert settled.related_runtime_authority is not None
    assert selected_after_convergence == selected_baseline

    scenario.script.set_authority(
        scenario.target_profile,
        b"{",
        _UNKNOWN_STATUS,
    )
    scenario.script.set_authority(
        scenario.native.config_directory,
        scenario.unknown_native_payload,
        _UNKNOWN_STATUS,
    )
    relation_failed = _recover(scenario, scenario.native_reconciliation)
    failed_dashboard = (
        CachedDashboardService(scenario.paths)
        .load(REFERENCE_TIME)
        .providers[0]
    )
    assert (
        relation_failed.outcome,
        scenario.selected.load(ProviderId.CLAUDE),
        failed_dashboard.runtime_state,
        failed_dashboard.active_account_id,
    ) == (
        WorkerOutcome.ACTION_REQUIRED,
        selected_after_convergence,
        ProviderRuntimeState.EXTERNAL_ACTIVE,
        None,
    )


def _assert_steady_native_reconciliation(
    scenario: ClaudeRecoveryScenario,
    saved_ids: tuple[SidekickAccountId, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove known, unknown, unchanged, and one raced native read-back."""
    login_profiles = list(scenario.script.login_profiles)
    selected_baseline = scenario.selected.load(ProviderId.CLAUDE)
    assert selected_baseline is not None
    observations = RuntimeAuthObservationStore(
        scenario.paths.durable_operations
    )
    scenario.script.set_authority(
        scenario.native.config_directory,
        scenario.known_native_payload,
        _KNOWN_STATUS,
    )
    steady_known = _recover(scenario, scenario.native_reconciliation)
    selected_known = scenario.selected.load(ProviderId.CLAUDE)
    assert steady_known.outcome is WorkerOutcome.SUCCEEDED
    assert steady_known.related_runtime_authority is not None
    assert steady_known.related_runtime_authority.account_id == (
        scenario.known.account_id
    )
    assert selected_known == selected_baseline
    known_observation = observations.observe_native(ProviderId.CLAUDE)
    assert known_observation is not None
    assert known_observation.state is ProviderAuthState.ACTIVE

    scenario.script.set_authority(
        scenario.native.config_directory,
        scenario.unknown_native_payload,
        _UNKNOWN_STATUS,
    )
    steady_unknown = _recover(scenario, scenario.native_reconciliation)
    selected_unknown = scenario.selected.load(ProviderId.CLAUDE)
    assert steady_unknown.outcome is WorkerOutcome.SUCCEEDED
    assert steady_unknown.related_runtime_authority is None
    assert selected_unknown == selected_baseline
    unknown_observation = observations.observe_native(ProviderId.CLAUDE)
    assert unknown_observation is not None
    assert unknown_observation.state is ProviderAuthState.ACTIVE
    assert unknown_observation != known_observation
    assert scenario.script.login_profiles == login_profiles
    assert (
        tuple(
            account.account_id for account in scenario.store.saved_accounts()
        )
        == saved_ids
    )
    unchanged = _recover(scenario, scenario.native_reconciliation)
    refreshed_selected = scenario.selected.load(ProviderId.CLAUDE)
    refreshed_observation = observations.observe_native(ProviderId.CLAUDE)
    assert unchanged.outcome is WorkerOutcome.SUCCEEDED
    assert unchanged.related_runtime_authority is None
    assert refreshed_selected == selected_baseline
    assert refreshed_observation == unknown_observation

    _assert_raced_native_reconciliation(
        scenario,
        observations,
        selected_baseline,
        monkeypatch,
    )


def _assert_exact_profile_proof_boundary(
    scenario: ClaudeRecoveryScenario,
    native_reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove exact status is required and changing proof never commits."""
    accounts_before = tuple(scenario.store.saved_accounts())
    login_profiles = list(scenario.script.login_profiles)
    selected_before = scenario.selected.load(ProviderId.CLAUDE)
    assert selected_before is not None
    assert selected_before.account_id == scenario.source.account_id
    scenario.script.set_status(
        scenario.native.config_directory,
        _INCOMPLETE_STATUS,
    )
    with pytest.raises(ClaudeProtectedStorageError) as incomplete:
        native_reader.read(
            capabilities,
            REFERENCE_TIME,
            environment=environment,
            runner=scenario.runner,
        )
    assert incomplete.value.code is (
        ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
    )

    incomplete_result = _recover(
        scenario,
        scenario.native_reconciliation,
    )
    assert (
        incomplete_result.outcome,
        scenario.selected.load(ProviderId.CLAUDE),
    ) == (WorkerOutcome.NO_CHANGE, selected_before)

    def reject_lock(_lock: PersistenceLock) -> None:
        raise AssertionError("Cached dashboard acquired a persistence lock.")

    with monkeypatch.context() as passive:
        passive.setattr(PersistenceLock, "hold", reject_lock)
        dashboard = CachedDashboardService(scenario.paths).load(REFERENCE_TIME)
    claude_dashboard = dashboard.providers[0]
    assert (
        claude_dashboard.provider_id,
        claude_dashboard.runtime_state,
        claude_dashboard.active_account_id,
        claude_dashboard.actions_enabled,
        tuple(scenario.store.saved_accounts()),
        scenario.script.login_profiles,
    ) == (
        ProviderId.CLAUDE,
        ProviderRuntimeState.UNREADABLE,
        None,
        False,
        accounts_before,
        login_profiles,
    )
    assert not any(
        isinstance(row, DashboardAccount) and row.active
        for row in claude_dashboard.rows
    )

    stable_payload = credential_payload(
        None,
        None,
        token_suffix=_STATUS_ONLY_TOKEN_SUFFIX,
        access_expires_at=REFERENCE_TIME + timedelta(hours=6),
    )
    scenario.script.set_authority(
        scenario.native.config_directory,
        stable_payload,
        _STATUS_ONLY_STATUS,
    )
    result = _recover(scenario, scenario.native_reconciliation)
    selected = scenario.selected.load(ProviderId.CLAUDE)
    observation = RuntimeAuthObservationStore(
        scenario.paths.durable_operations
    ).observe_native(ProviderId.CLAUDE)

    assert observation is not None
    assert (
        result.outcome,
        result.related_runtime_authority,
        selected,
        observation.state,
        observation.generation,
        scenario.native_credentials.read_bytes(),
        scenario.script.login_profiles,
        tuple(scenario.store.saved_accounts()),
    ) == (
        WorkerOutcome.SUCCEEDED,
        None,
        selected_before,
        ProviderAuthState.ACTIVE,
        claude_access_token_generation(
            f"sk-ant-oat01-{_STATUS_ONLY_TOKEN_SUFFIX}"
        ),
        stable_payload,
        login_profiles,
        accounts_before,
    )

    race_states = cycle(
        (
            (scenario.unknown_native_payload, _UNKNOWN_STATUS),
            (stable_payload, _STATUS_ONLY_STATUS),
        )
    )

    def change_during_status(
        arguments: tuple[str, ...],
        process_environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        if arguments == ("auth", "status"):
            payload, status = next(race_states)
            scenario.script.set_authority(
                scenario.native.config_directory,
                payload,
                status,
            )
        return scenario.script(
            arguments,
            process_environment,
            working_directory,
        )

    scenario.script.set_authority(
        scenario.native.config_directory,
        stable_payload,
        _STATUS_ONLY_STATUS,
    )
    racing_runner = ClaudeRunner(script=change_during_status)
    for _ in range(2):
        with pytest.raises(ClaudeProtectedStorageError) as changed:
            native_reader.read(
                capabilities,
                REFERENCE_TIME,
                environment=environment,
                runner=racing_runner,
            )
        assert changed.value.code is (
            ClaudeProtectedStorageFailure.PROOF_CHANGED
        )
    assert tuple(scenario.store.saved_accounts()) == accounts_before


def test_interrupted_native_activation_rolls_back_once_or_requires_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery performs one official rollback and never loops on failure."""
    use_synthetic_claude(monkeypatch)
    recovered = claude_recovery_scenario(
        tmp_path / "recovered",
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    recovered_baseline = recovered.selected.load(ProviderId.CLAUDE)
    assert recovered_baseline is not None
    _interrupt(recovered)
    interrupted = recovered.journals.load(ProviderId.CLAUDE)
    assert interrupted.active is not None
    assert interrupted.active.phase is ActivationPhase.OUTGOING_RETAINED

    result = _recover(recovered, recovered.recovery)
    retry = _recover(recovered, recovered.retry)

    assert result.outcome is WorkerOutcome.SUCCEEDED
    assert retry.outcome is WorkerOutcome.SUCCEEDED
    selected = recovered.selected.load(ProviderId.CLAUDE)
    assert selected == recovered_baseline
    assert (
        recovered.native_credentials.read_bytes()
        == recovered.native_rollback_payload
    )
    assert recovered.native_rollback_payload != (
        recovered.retained_source_payload
    )
    recovered_source = recovered.profiles.read_owned_file(
        recovered.source_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    recovered_target = recovered.profiles.read_owned_file(
        recovered.target_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    assert recovered_source is not None
    assert recovered_source.data == recovered.retained_source_payload
    assert recovered_target is not None
    assert recovered_target.data == recovered.target_payload
    assert (
        recovered.script.login_profiles.count(
            recovered.native.config_directory
        )
        == _EXPECTED_NATIVE_LOGINS
    )
    recovered_journal = recovered.journals.load(ProviderId.CLAUDE)
    assert recovered_journal.active is None
    assert recovered_journal.history[-1].outcome is (
        ActivationOutcome.ROLLED_BACK
    )
    assert recovered.selected.load(ProviderId.CODEX) == recovered.codex_state

    blocked = claude_recovery_scenario(
        tmp_path / "blocked",
        _SimulatedCrash(),
        rollback_succeeds=False,
    )
    _interrupt(blocked)
    failure = _recover(blocked, blocked.recovery)
    repeated = _recover(blocked, blocked.retry)

    assert failure.outcome is WorkerOutcome.ACTION_REQUIRED
    assert repeated.outcome is WorkerOutcome.ACTION_REQUIRED
    assert (
        blocked.script.login_profiles.count(blocked.native.config_directory)
        == _EXPECTED_NATIVE_LOGINS
    )
    blocked_source = blocked.profiles.read_owned_file(
        blocked.source_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    blocked_target = blocked.profiles.read_owned_file(
        blocked.target_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    assert blocked_source is not None
    assert blocked_source.data == blocked.retained_source_payload
    assert blocked_target is not None
    assert blocked_target.data == blocked.target_payload
    assert (
        blocked.native_credentials.read_bytes()
        == blocked.native_target_payload
    )
    blocked_journal = blocked.journals.load(ProviderId.CLAUDE)
    assert blocked_journal.active is not None
    assert (
        blocked_journal.active.phase is ActivationPhase.RECONCILIATION_REQUIRED
    )
    blocked_selected = blocked.selected.load(ProviderId.CLAUDE)
    assert blocked_selected is not None
    assert blocked_selected.account_id == blocked.source.account_id
    assert blocked.selected.load(ProviderId.CODEX) == blocked.codex_state


def _assert_repairable_external_recovery(root: Path) -> None:
    """Require interrupted outgoing retention to roll back exactly."""
    repairable = claude_recovery_scenario(
        root,
        ClaudeActivationError(ClaudeActivationFailure.RECONCILIATION_REQUIRED),
        rollback_succeeds=True,
    )
    with ProviderMutationLock(
        repairable.paths.durable_operations,
        ProviderId.CLAUDE,
        (
            repairable.source.account_id,
            repairable.target.account_id,
        ),
        timeout_seconds=1.0,
    ).hold() as authority:
        interrupted = repairable.executor.execute(
            repairable.activation,
            authority,
        )
    repair_journal = repairable.journals.load(ProviderId.CLAUDE)
    repair_active = repair_journal.active
    assert repair_active is not None
    assert (
        interrupted.outcome,
        repair_active.phase,
        repair_active.reconciliation_origin_phase,
    ) == (
        WorkerOutcome.ACTION_REQUIRED,
        ActivationPhase.RECONCILIATION_REQUIRED,
        ActivationPhase.OUTGOING_RETAINED,
    )

    repaired = _recover(repairable, repairable.recovery)
    repaired_selected = repairable.selected.load(ProviderId.CLAUDE)
    assert repaired_selected is not None
    repaired_journal = repairable.journals.load(ProviderId.CLAUDE)
    assert (
        repaired.outcome,
        repaired_selected.account_id,
        repaired_journal.history[-1].outcome,
        repaired_journal.active,
    ) == (
        WorkerOutcome.SUCCEEDED,
        repairable.source.account_id,
        ActivationOutcome.ROLLED_BACK,
        None,
    )


def _assert_known_external_login(root: Path) -> None:
    """Require a known native login to produce saved-account relation."""
    known = claude_recovery_scenario(
        root,
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    known_baseline = known.selected.load(ProviderId.CLAUDE)
    assert known_baseline is not None
    _interrupt(known)
    known.script.set_authority(
        known.native.config_directory,
        known.known_native_payload,
        _KNOWN_STATUS,
    )
    known_result = _recover(known, known.native_reconciliation)

    known_selected = known.selected.load(ProviderId.CLAUDE)
    assert known_result.related_runtime_authority is not None
    assert (
        known_result.outcome,
        known_selected,
        known_result.related_runtime_authority.account_id,
    ) == (
        WorkerOutcome.SUCCEEDED,
        known_baseline,
        known.known.account_id,
    )
    assert known.native_credentials.read_bytes() == known.known_native_payload
    assert known.script.login_profiles == [
        known.source_profile,
        known.native.config_directory,
    ]
    assert known.journals.load(ProviderId.CLAUDE).active is None
    assert known.selected.load(ProviderId.CODEX) == known.codex_state
    known_dashboard = CachedDashboardService(known.paths).load(REFERENCE_TIME)
    known_claude = known_dashboard.providers[0]
    known_controller = DashboardController.start(known_dashboard)
    assert (
        known_claude.active_account_id,
        known_claude.status.unmanaged_sessions,
        known_controller.state.focused_provider,
        known_controller.state.account_id,
    ) == (
        None,
        None,
        ProviderId.CLAUDE,
        known.source.account_id,
    )


def _assert_unknown_external_login(
    root: Path,
) -> tuple[ClaudeRecoveryScenario, tuple[SidekickAccountId, ...]]:
    """Require unknown native identity to remain observed but unsaved."""
    unknown = claude_recovery_scenario(
        root,
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    unknown_baseline = unknown.selected.load(ProviderId.CLAUDE)
    assert unknown_baseline is not None
    saved_ids = tuple(
        account.account_id for account in unknown.store.saved_accounts()
    )
    _interrupt(unknown)
    unknown.script.set_authority(
        unknown.native.config_directory,
        unknown.unknown_native_payload,
        _UNKNOWN_STATUS,
    )
    unknown_result = _recover(unknown, unknown.recovery)

    assert unknown_result.outcome is WorkerOutcome.SUCCEEDED
    unknown_selected = unknown.selected.load(ProviderId.CLAUDE)
    assert unknown_selected == unknown_baseline
    unknown_observation = RuntimeAuthObservationStore(
        unknown.paths.durable_operations
    ).observe_native(ProviderId.CLAUDE)
    assert unknown_observation is not None
    assert unknown_observation.state is ProviderAuthState.ACTIVE
    assert unknown_result.related_runtime_authority is None
    assert (
        tuple(account.account_id for account in unknown.store.saved_accounts())
        == saved_ids
    )
    assert (
        unknown.native_credentials.read_bytes()
        == unknown.unknown_native_payload
    )
    unknown_journal = unknown.journals.load(ProviderId.CLAUDE)
    assert unknown_journal.active is None
    assert unknown_journal.history[-1].outcome is (
        ActivationOutcome.EXTERNAL_RECONCILED
    )
    assert unknown.selected.load(ProviderId.CODEX) == unknown.codex_state
    return unknown, saved_ids


def test_external_claude_login_wins_without_importing_unknown_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known external login is related; unknown login remains unsaved."""
    use_synthetic_claude(monkeypatch)
    _assert_repairable_external_recovery(tmp_path / "repairable")
    _assert_known_external_login(tmp_path / "known")
    unknown, saved_ids = _assert_unknown_external_login(tmp_path / "unknown")

    status_only = claude_recovery_scenario(
        tmp_path / "status-only",
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    native_reader = ClaudeNativeAuthorityReader(status_only.native)
    native_capabilities = claude_capabilities(
        status_only.native,
        ClaudeManagedPlatform.LINUX_FILE,
    )
    native_environment = {
        "HOME": str(status_only.native.config_directory.parent),
        "PATH": os.defpath,
        "USER": "sidekick-test",
    }
    _assert_exact_profile_proof_boundary(
        status_only,
        native_reader,
        native_capabilities,
        native_environment,
        monkeypatch,
    )

    _assert_steady_native_reconciliation(
        unknown,
        saved_ids,
        monkeypatch,
    )
