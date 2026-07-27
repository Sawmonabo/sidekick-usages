"""Load-bearing Claude activation recovery scenarios."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from tests.fakes.claude.activation import (
    ClaudeRecoveryScenario,
    claude_recovery_scenario,
)
from tests.fakes.claude.managed import (
    credential_payload,
    use_synthetic_claude,
)
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME

pytestmark = REQUIRES_MANAGED_RUNTIME
_EXPECTED_NATIVE_LOGINS = 2
_STATUS_ONLY_TOKEN_SUFFIX = "status-only-native"


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


def _assert_steady_native_reconciliation(
    scenario: ClaudeRecoveryScenario,
    saved_ids: tuple[SidekickAccountId, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove known, unknown, unchanged, and one raced native read-back."""
    login_profiles = list(scenario.script.login_profiles)
    scenario.native_credentials.write_bytes(scenario.known_native_payload)
    steady_known = _recover(scenario, scenario.native_reconciliation)
    selected_known = scenario.selected.load(ProviderId.CLAUDE)
    assert steady_known.outcome is WorkerOutcome.SUCCEEDED
    assert selected_known is not None
    assert selected_known.account_id == scenario.known.account_id

    scenario.native_credentials.write_bytes(scenario.unknown_native_payload)
    steady_unknown = _recover(scenario, scenario.native_reconciliation)
    selected_unknown = scenario.selected.load(ProviderId.CLAUDE)
    assert steady_unknown.outcome is WorkerOutcome.SUCCEEDED
    assert selected_unknown is not None
    assert selected_unknown.runtime_state is (
        ProviderRuntimeState.EXTERNAL_ACTIVE
    )
    assert selected_unknown.account_id is None
    assert scenario.script.login_profiles == login_profiles
    assert (
        tuple(
            account.account_id for account in scenario.store.saved_accounts()
        )
        == saved_ids
    )
    stale_selected = replace(
        selected_unknown,
        verified_at=selected_unknown.verified_at - timedelta(seconds=1),
    )
    scenario.selected.save(stale_selected)
    unchanged = _recover(scenario, scenario.native_reconciliation)
    refreshed_selected = scenario.selected.load(ProviderId.CLAUDE)
    assert unchanged.outcome is WorkerOutcome.NO_CHANGE
    assert refreshed_selected is not None
    assert refreshed_selected.verified_at > stale_selected.verified_at

    original_read = PersistenceFilesystem.read_opaque_private
    native_reads = 0

    def change_native_after_first_read(
        filesystem: PersistenceFilesystem,
    ) -> FileSnapshot | None:
        nonlocal native_reads
        snapshot = original_read(filesystem)
        if (
            filesystem.authority_path == scenario.native_credentials
            and native_reads == 0
        ):
            native_reads += 1
            scenario.native_credentials.write_bytes(
                scenario.known_native_payload
            )
        return snapshot

    monkeypatch.setattr(
        PersistenceFilesystem,
        "read_opaque_private",
        change_native_after_first_read,
    )
    raced = _recover(scenario, scenario.native_reconciliation)
    selected_after_race = scenario.selected.load(ProviderId.CLAUDE)
    assert raced.outcome is WorkerOutcome.SUCCEEDED
    assert native_reads == 1
    assert selected_after_race is not None
    assert selected_after_race.account_id == scenario.known.account_id


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
    _interrupt(recovered)
    interrupted = recovered.journals.load(ProviderId.CLAUDE)
    assert interrupted.active is not None
    assert interrupted.active.phase is ActivationPhase.OUTGOING_RETAINED

    result = _recover(recovered, recovered.recovery)
    retry = _recover(recovered, recovered.retry)

    assert result.outcome is WorkerOutcome.SUCCEEDED
    assert retry.outcome is WorkerOutcome.SUCCEEDED
    selected = recovered.selected.load(ProviderId.CLAUDE)
    assert selected is not None
    assert selected.account_id == recovered.source.account_id
    assert selected.outcome is ActivationOutcome.ROLLED_BACK
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
    assert recovered.journals.load(ProviderId.CLAUDE).active is None
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


def test_external_claude_login_wins_without_importing_unknown_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known external login is related; unknown login remains unsaved."""
    use_synthetic_claude(monkeypatch)
    known = claude_recovery_scenario(
        tmp_path / "known",
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    _interrupt(known)
    known.native_credentials.write_bytes(known.known_native_payload)
    known_result = _recover(known, known.native_reconciliation)

    assert known_result.outcome is WorkerOutcome.SUCCEEDED
    known_selected = known.selected.load(ProviderId.CLAUDE)
    assert known_selected is not None
    assert known_selected.account_id == known.known.account_id
    assert known_selected.outcome is ActivationOutcome.EXTERNAL_RECONCILED
    assert known.native_credentials.read_bytes() == known.known_native_payload
    assert known.script.login_profiles == [
        known.source_profile,
        known.native.config_directory,
    ]
    assert known.journals.load(ProviderId.CLAUDE).active is None
    assert known.selected.load(ProviderId.CODEX) == known.codex_state

    unknown = claude_recovery_scenario(
        tmp_path / "unknown",
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    saved_ids = tuple(
        account.account_id for account in unknown.store.saved_accounts()
    )
    _interrupt(unknown)
    unknown.native_credentials.write_bytes(unknown.unknown_native_payload)
    unknown_result = _recover(unknown, unknown.recovery)

    assert unknown_result.outcome is WorkerOutcome.SUCCEEDED
    unknown_selected = unknown.selected.load(ProviderId.CLAUDE)
    assert unknown_selected is not None
    assert unknown_selected.runtime_state is (
        ProviderRuntimeState.EXTERNAL_ACTIVE
    )
    assert unknown_selected.account_id is None
    assert unknown_selected.outcome is ActivationOutcome.EXTERNAL_RECONCILED
    assert (
        tuple(account.account_id for account in unknown.store.saved_accounts())
        == saved_ids
    )
    assert (
        unknown.native_credentials.read_bytes()
        == unknown.unknown_native_payload
    )
    assert unknown.journals.load(ProviderId.CLAUDE).active is None
    assert unknown.selected.load(ProviderId.CODEX) == unknown.codex_state

    status_only = claude_recovery_scenario(
        tmp_path / "status-only",
        _SimulatedCrash(),
        rollback_succeeds=True,
    )
    status_only_payload = credential_payload(
        None,
        None,
        token_suffix=_STATUS_ONLY_TOKEN_SUFFIX,
        access_expires_at=REFERENCE_TIME + timedelta(hours=6),
    )
    status_only.native_credentials.write_bytes(status_only_payload)
    status_saved_ids = tuple(
        account.account_id for account in status_only.store.saved_accounts()
    )
    status_login_profiles = list(status_only.script.login_profiles)

    status_result = _recover(
        status_only,
        status_only.native_reconciliation,
    )
    status_selected = status_only.selected.load(ProviderId.CLAUDE)

    assert status_result.outcome is WorkerOutcome.SUCCEEDED
    assert status_selected is not None
    assert status_selected.runtime_state is (
        ProviderRuntimeState.EXTERNAL_ACTIVE
    )
    assert status_selected.account_id is None
    assert status_selected.runtime_generation == (
        claude_access_token_generation(
            f"sk-ant-oat01-{_STATUS_ONLY_TOKEN_SUFFIX}"
        )
    )
    assert status_only.native_credentials.read_bytes() == status_only_payload
    assert status_only.script.login_profiles == status_login_profiles
    assert (
        tuple(
            account.account_id
            for account in status_only.store.saved_accounts()
        )
        == status_saved_ids
    )

    _assert_steady_native_reconciliation(
        unknown,
        saved_ids,
        monkeypatch,
    )
