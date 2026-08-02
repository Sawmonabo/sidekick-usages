"""Load-bearing native Claude activation scenario."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
)
from sidekick_usages.core.selection.models import (
    ClaudeAuthObservation,
    DueOperation,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationFailure,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
    ClaudeForegroundState,
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
from sidekick_usages.usage.dashboard.models import DashboardAccount
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.fakes.claude.activation import (
    ClaudeActivationScenario,
    claude_activation_scenario,
)
from tests.fakes.claude.managed import use_synthetic_claude
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

pytestmark = REQUIRES_MANAGED_RUNTIME


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
    rejected = _execute_activation(conflict, conflict.operation)

    assert rejected.outcome is WorkerOutcome.ACTION_REQUIRED
    assert rejected.failure_code == (
        ClaudeActivationGuardFailure.ANTHROPIC_API_KEY.failure_code
    )
    assert conflict_environment == original_environment
    assert conflict.runner.calls == []
    assert conflict.script.login_profiles == []
    assert conflict.journals.load(ProviderId.CLAUDE).active is None

    scenario = claude_activation_scenario(
        root / "foreground",
        foreground=ClaudeForegroundState.PRESENT,
    )
    native_before = scenario.native_credentials.read_bytes()
    selected_before = scenario.selected.load(ProviderId.CLAUDE)
    refused = _execute_activation(scenario, scenario.operation)

    assert refused.outcome is WorkerOutcome.ACTION_REQUIRED
    assert (
        refused.failure_code
        == (
            ClaudeActivationGuardFailure.REMOTE_CONTROL_DISCONNECT_REQUIRED
        ).failure_code
    )
    assert scenario.native_credentials.read_bytes() == native_before
    assert scenario.selected.load(ProviderId.CLAUDE) == selected_before
    assert scenario.script.login_profiles == []
    assert scenario.journals.load(ProviderId.CLAUDE).active is None
    return scenario


def test_native_activation_retains_source_and_commits_verified_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards fail closed before one approved, provider-proven switch."""
    use_synthetic_claude(monkeypatch)
    scenario = _guarded_activation_scenario(tmp_path)
    result = _execute_activation(
        scenario,
        replace(
            scenario.operation,
            allow_remote_control_disconnect=True,
        ),
    )

    assert result.outcome is WorkerOutcome.SUCCEEDED
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
        replace(
            status_only.operation,
            allow_remote_control_disconnect=True,
        ),
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
        replace(
            unpropagated.operation,
            allow_remote_control_disconnect=True,
        ),
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
