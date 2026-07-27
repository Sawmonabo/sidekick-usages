"""Load-bearing managed Codex activation tests."""

from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import (
    CompletedPayload,
    FailedPayload,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import CodexDaemonAuthority
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
)
from tests.fakes.codex.broker.runtime import (
    ACCOUNT_A_ID,
    ACCOUNT_A_PROVIDER_IDENTITY,
    GENERATION,
    MANAGED_ACCOUNT_ID,
    NATIVE_AUTH_SENTINEL,
    NEXT_GENERATION,
    PROVIDER_IDENTITY,
    UNKNOWN_GENERATION,
    UNKNOWN_PROVIDER_IDENTITY,
    UNSELECTED_NEXT_GENERATION,
    account_store,
    broker_fixture,
    interrupt_activation_at_install,
    real_worker_executable,
    selected_account,
    wait_for_external_selection,
)
from tests.fakes.codex.broker.supervisor import FakeCodexSupervisor
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME

pytestmark = REQUIRES_MANAGED_RUNTIME

_CLAUDE_ACCOUNT_ID = SidekickAccountId("55555555-5555-4555-8555-555555555555")
_FIRST_ACTIVATION_ID = OperationId("88888888-8888-4888-8888-888888888888")
_SECOND_ACTIVATION_ID = OperationId("99999999-9999-4999-8999-999999999999")


def _require_selected(
    selected: SelectedStateStore,
    account_id: SidekickAccountId,
    provider_identity: str,
    generation: str,
) -> SelectedAccountState:
    state = selected.load(ProviderId.CODEX)
    assert state is not None
    assert state.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
    assert state.account_id == account_id
    assert state.provider_identity == ProviderIdentity(provider_identity)
    assert state.runtime_generation == AuthorityGeneration(generation)
    return state


def _assert_fresh_codex_reconciliation(
    socket_path: Path,
    daemon: FakeCodexDaemon,
    selected: SelectedStateStore,
) -> None:
    """Require each native operation to read its current runtime first."""
    reads_before = daemon.auth_status_read_count
    selected_before = selected.load(ProviderId.CODEX)
    assert selected_before is not None
    client = ControlClient.connect(socket_path)
    events = tuple(client.reconcile(ProviderId.CODEX))
    client.close()
    selected_after = selected.load(ProviderId.CODEX)
    assert events[-1].kind is EventKind.COMPLETED
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-1].payload.outcome is CompletionOutcome.NO_CHANGE
    assert daemon.auth_status_read_count > reads_before
    assert selected_after is not None
    assert selected_after.verified_at > selected_before.verified_at


def _codex_recovery_state(
    paths: ApplicationPaths,
) -> tuple[SelectedStateStore, ActivationJournalStore]:
    """Seed the selected baseline and return both recovery stores."""
    selected = SelectedStateStore(paths.selected_state)
    selected.save(
        selected_account(
            ACCOUNT_A_ID,
            ACCOUNT_A_PROVIDER_IDENTITY,
            GENERATION,
        )
    )
    return (
        selected,
        ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        ),
    )


def test_codex_activation_commits_only_correlated_target(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = broker_fixture(tmp_path, short_socket_root, monkeypatch)
    selected = SelectedStateStore(fixture.paths.selected_state)
    claude = SelectedAccountState(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        account_id=_CLAUDE_ACCOUNT_ID,
        provider_identity=ProviderIdentity("claude-workspace"),
        runtime_generation=AuthorityGeneration("claude-generation"),
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.VERIFIED,
    )
    selected.save(claude)
    reject_revalidation = False
    revalidate = CodexDaemonManager.revalidate

    def revalidate_current_socket(
        manager: CodexDaemonManager,
        authority: CodexDaemonAuthority,
    ) -> None:
        if reject_revalidation:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        revalidate(manager, authority)

    monkeypatch.setattr(
        CodexDaemonManager,
        "revalidate",
        revalidate_current_socket,
    )

    with FakeCodexDaemon(fixture.native_home) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.native_home,
            daemon.socket_path,
        )
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            selected_baseline = selected.load(ProviderId.CODEX)
            assert selected_baseline is not None
            install_count = len(daemon.installed_account_ids)
            reject_revalidation = True
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            failed = tuple(
                client.activate(ProviderId.CODEX, MANAGED_ACCOUNT_ID)
            )
            client.close()

            assert [event.kind for event in failed] == [
                EventKind.ACCEPTED,
                EventKind.PROGRESS,
                EventKind.FAILED,
            ]
            assert isinstance(failed[-1].payload, FailedPayload)
            assert len(daemon.installed_account_ids) > install_count
            assert selected.load(ProviderId.CODEX) == selected_baseline
            assert selected.load(ProviderId.CLAUDE) == claude
            interrupted = ActivationJournalStore(
                fixture.paths.activation_journals,
                fixture.paths.durable_operations,
            ).load(ProviderId.CODEX)
            assert interrupted.active is not None
            assert (
                interrupted.active.phase
                is ActivationPhase.RECONCILIATION_REQUIRED
            )

        reject_revalidation = False
        daemon.pause_next_install()
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            daemon.wait_for_paused_install()
            daemon.resume_install()
            restarted.wait_until_ready()
            _require_selected(
                selected,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            journal = ActivationJournalStore(
                fixture.paths.activation_journals,
                fixture.paths.durable_operations,
            ).load(ProviderId.CODEX)
            assert journal.active is None
            assert journal.history[-1].phase is ActivationPhase.COMMITTED
            assert journal.history[-1].target_authority_generation == (
                AuthorityGeneration(NEXT_GENERATION)
            )
            assert journal.history[-1].verified_runtime_generation == (
                AuthorityGeneration(NEXT_GENERATION)
            )
            assert selected.load(ProviderId.CLAUDE) == claude

    assert fixture.native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_codex_activation_recovers_at_official_mutation_boundary(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = broker_fixture(tmp_path, short_socket_root, monkeypatch)
    selected, journals = _codex_recovery_state(fixture.paths)

    with FakeCodexDaemon(fixture.native_home) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.native_home,
            daemon.socket_path,
        )
        supervisor = FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        )
        supervisor.start()
        supervisor.wait_until_ready()
        interrupt_activation_at_install(
            supervisor,
            daemon,
            fixture.paths,
            _FIRST_ACTIVATION_ID,
            MANAGED_ACCOUNT_ID,
        )

        assert daemon.installed_account_ids[-1] == PROVIDER_IDENTITY
        _require_selected(
            selected,
            ACCOUNT_A_ID,
            ACCOUNT_A_PROVIDER_IDENTITY,
            GENERATION,
        )
        assert journals.load(ProviderId.CODEX).active is not None
        installed_before_recovery = len(daemon.installed_account_ids)

        daemon.pause_next_install()
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            daemon.wait_for_paused_install()
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            retry = client.reconcile(ProviderId.CODEX)
            accepted = next(retry)
            assert accepted.kind is EventKind.ACCEPTED
            daemon.resume_install()
            assert tuple(retry)[-1].kind is EventKind.COMPLETED
            client.close()
            restarted.wait_until_ready()
            _assert_fresh_codex_reconciliation(
                fixture.paths.supervisor_socket,
                daemon,
                selected,
            )

            assert (
                len(daemon.installed_account_ids) > installed_before_recovery
            )
            _require_selected(
                selected,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            recovered = journals.load(ProviderId.CODEX)
            assert recovered.active is None
            assert len(recovered.history) == 1

            interrupt_activation_at_install(
                restarted,
                daemon,
                fixture.paths,
                _SECOND_ACTIVATION_ID,
                ACCOUNT_A_ID,
            )
            account_a_installs = daemon.installed_account_ids.count(
                ACCOUNT_A_PROVIDER_IDENTITY
            )
            account_b_installs = daemon.installed_account_ids.count(
                PROVIDER_IDENTITY
            )

        daemon.perform_external_runtime_login(
            PROVIDER_IDENTITY,
            NEXT_GENERATION,
        )
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as external_recovery:
            external_recovery.wait_until_ready()
            assert (
                daemon.installed_account_ids.count(ACCOUNT_A_PROVIDER_IDENTITY)
                == account_a_installs
            )
            assert (
                daemon.installed_account_ids.count(PROVIDER_IDENTITY)
                > account_b_installs
            )
            _require_selected(
                selected,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            reconciled = journals.load(ProviderId.CODEX)
            assert reconciled.active is None
            assert tuple(record.outcome for record in reconciled.history) == (
                ActivationOutcome.VERIFIED,
                ActivationOutcome.ROLLED_BACK,
            )
            rollback = reconciled.history[-1]
            assert (
                rollback.target_authority_generation,
                rollback.verified_runtime_generation,
            ) == (
                AuthorityGeneration(UNSELECTED_NEXT_GENERATION),
                AuthorityGeneration(NEXT_GENERATION),
            )
            saved_ids = tuple(
                account.account_id
                for account in account_store(fixture.paths).saved_accounts()
            )
            daemon.perform_external_runtime_login(
                UNKNOWN_PROVIDER_IDENTITY,
                UNKNOWN_GENERATION,
            )
            external = wait_for_external_selection(
                fixture.paths,
                UNKNOWN_PROVIDER_IDENTITY,
            )
            assert external.account_id is None
            assert (
                tuple(
                    account.account_id
                    for account in account_store(
                        fixture.paths
                    ).saved_accounts()
                )
                == saved_ids
            )
            external_recovery.wait_until_ready()
