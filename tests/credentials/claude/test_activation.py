"""Load-bearing native Claude activation scenario."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import ActivationPhase
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
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
    assert isinstance(
        current_source_subscription,
        ClaudeManagedLoginAuthority,
    )
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
    assert retained is not None
    assert retained.data == scenario.retained_source_payload
    assert unchanged_target is not None
    assert unchanged_target.data == scenario.target_payload
    assert (
        scenario.native_credentials.read_bytes()
        == scenario.native_target_payload
    )
    assert scenario.script.login_profiles == [
        scenario.source_profile,
        scenario.native.config_directory,
    ]
    login_environments = [
        environment
        for (_executable, arguments), environment in zip(
            scenario.runner.calls,
            scenario.runner.environments,
            strict=True,
        )
        if arguments == ("auth", "login", "--claudeai")
    ]
    assert login_environments[0] is not None
    assert login_environments[1] is not None
    assert login_environments[0][CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY] == str(
        scenario.source_profile
    )
    assert CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY not in login_environments[1]
    claude_state = scenario.selected.load(ProviderId.CLAUDE)
    assert claude_state is not None
    assert claude_state.account_id == scenario.target.account_id
    assert claude_state.provider_identity == (
        scenario.target.provider_identity
    )
    assert scenario.selected.load(ProviderId.CODEX) == scenario.codex_state
    journal = scenario.journals.load(ProviderId.CLAUDE)
    assert journal.active is None
    assert len(journal.history) == 1
    committed = journal.history[0]
    assert committed.phase is ActivationPhase.COMMITTED
    assert committed.target_account_id == scenario.target.account_id
    assert (
        committed.verified_runtime_generation
        == claude_state.runtime_generation
    )
