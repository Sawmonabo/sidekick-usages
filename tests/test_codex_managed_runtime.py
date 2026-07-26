"""Load-bearing tests for the versioned managed Codex runtime."""

import os
import sys
import time
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    FailedPayload,
    ProgressPayload,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_ERROR_CODE,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.daemon import FakeCodexDaemon
from tests.fakes.codex.executable import (
    RAW_PROVIDER_SECRET,
    configure_codex_daemon_lifecycle,
    write_fake_managed_codex,
    write_worker_router,
)
from tests.fakes.codex.managed import (
    managed_generation,
    managed_saved_account,
    seed_managed_accounts,
)
from tests.fakes.codex.runtime import (
    ACCOUNT_A_ID,
    ACCOUNT_A_PROVIDER_IDENTITY,
    GENERATION,
    MANAGED_ACCOUNT_ID,
    MANAGED_AUTHORITY_ID,
    NATIVE_AUTH_SENTINEL,
    NEXT_GENERATION,
    PROVIDER_IDENTITY,
    RECOVERY_GENERATION,
    UNKNOWN_GENERATION,
    UNKNOWN_PROVIDER_IDENTITY,
    UNSELECTED_NEXT_GENERATION,
    account_store,
    broker_fixture,
    interrupt_activation_at_install,
    real_worker_executable,
    saved_generation,
    selected_account,
    stage_provider_ahead,
    wait_for_external_selection,
    wait_for_file,
    wait_for_operation_state,
    wait_for_selected_generation,
)
from tests.fakes.codex.schema import write_codex_schema
from tests.fakes.codex.supervisor import FakeCodexSupervisor
from tests.test_support import REFERENCE_TIME, FixedClock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Managed Codex runtimes require Linux, WSL, or macOS.",
)

_CLAUDE_ACCOUNT_ID = SidekickAccountId("55555555-5555-4555-8555-555555555555")
_MAINTENANCE_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
_FIRST_ACTIVATION_ID = OperationId("88888888-8888-4888-8888-888888888888")
_SECOND_ACTIVATION_ID = OperationId("99999999-9999-4999-8999-999999999999")
_IDLE_BROKER_OBSERVATION_SECONDS = 0.6
_CALLBACK_RESPONSE_BOUND_SECONDS = 8.0
_INITIAL_LIFECYCLE_CALLS = 2
_RECOVERED_LIFECYCLE_CALLS = 3
_INITIAL_READY_READS = 1
_REHYDRATED_READY_READS = 2


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


def test_shared_codex_runtime_is_idempotent_and_rehydrates(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    schema_root = tmp_path / "schema"
    native_home = short_socket_root / "native"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(NATIVE_AUTH_SENTINEL)
    os.chmod(native_auth, 0o600)
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(tmp_path, schema_root, native_home)
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    account = managed_saved_account(
        MANAGED_ACCOUNT_ID,
        MANAGED_AUTHORITY_ID,
        "codex-alpha",
        PROVIDER_IDENTITY,
        GENERATION,
    )
    paths, store, private = seed_managed_accounts(
        tmp_path,
        (account,),
        {
            MANAGED_ACCOUNT_ID: managed_auth(
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
        },
    )
    coordinator = CodexManagedAuthorityCoordinator(
        paths,
        store,
        private,
        capabilities,
        FixedClock(),
        environment=environment,
    )

    with FakeCodexDaemon(native_home) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            tmp_path,
            native_home,
            daemon.socket_path,
        )
        runtime = CodexSharedRuntime.create(
            executable,
            native_home,
            environment=environment,
        )
        observer_a = daemon.connect_tui()
        observer_b = daemon.connect_tui()

        first = coordinator.install_current_projection(
            MANAGED_ACCOUNT_ID,
            runtime,
        )
        second = coordinator.install_current_projection(
            MANAGED_ACCOUNT_ID,
            runtime,
        )

        assert second == first
        assert runtime.ready
        observer_a.wait_for_account_update()
        observer_b.wait_for_account_update()
        assert daemon.installed_account_ids == (PROVIDER_IDENTITY,)
        assert daemon.ready_account_read_count == _INITIAL_READY_READS
        assert lifecycle.start_statuses == ("started", "alreadyRunning")
        assert lifecycle.version_count == _INITIAL_LIFECYCLE_CALLS

        daemon.replace()
        assert not runtime.ready
        observer_after_replacement = daemon.connect_tui()
        coordinator.install_current_projection(
            MANAGED_ACCOUNT_ID,
            runtime,
        )

        assert runtime.ready
        observer_after_replacement.wait_for_account_update()
        assert daemon.installed_account_ids == (
            PROVIDER_IDENTITY,
            PROVIDER_IDENTITY,
        )
        assert daemon.ready_account_read_count == _REHYDRATED_READY_READS
        assert lifecycle.start_statuses == (
            "started",
            "alreadyRunning",
            "alreadyRunning",
        )
        assert lifecycle.version_count == _RECOVERED_LIFECYCLE_CALLS

        observer_a.close()
        observer_b.close()
        observer_after_replacement.close()
        runtime.close()

    assert native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_resident_broker_refreshes_and_recovers_provider_ahead_state(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resident broker refreshes B and repairs a provider-ahead restart."""
    fixture = broker_fixture(tmp_path, short_socket_root, monkeypatch)
    paths = fixture.paths

    with FakeCodexDaemon(fixture.native_home) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.native_home,
            daemon.socket_path,
        )
        observer_a = daemon.connect_tui()
        observer_b = daemon.connect_tui()
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()
            dashboard = ControlClient.connect(paths.supervisor_socket)
            try:
                dashboard.handshake()
            finally:
                dashboard.close()

            rejected = daemon.request_refresh("unknown-provider-account")
            assert rejected.responder == "sidekick_usages"
            assert rejected.error_code == CODEX_REFRESH_ERROR_CODE
            refreshed = daemon.request_refresh(PROVIDER_IDENTITY)
            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == PROVIDER_IDENTITY
            wait_for_selected_generation(paths, NEXT_GENERATION)
            assert (
                saved_generation(paths, MANAGED_ACCOUNT_ID) == NEXT_GENERATION
            )
            assert saved_generation(paths, ACCOUNT_A_ID) == GENERATION
            initial_starts = lifecycle.start_statuses
            initial_versions = lifecycle.version_count
            time.sleep(_IDLE_BROKER_OBSERVATION_SECONDS)
            assert lifecycle.start_statuses == initial_starts
            assert lifecycle.version_count == initial_versions

        stage_provider_ahead(fixture)
        assert (
            managed_generation(fixture.private, MANAGED_ACCOUNT_ID)
            == RECOVERY_GENERATION
        )
        assert saved_generation(paths, MANAGED_ACCOUNT_ID) == NEXT_GENERATION
        wait_for_selected_generation(paths, NEXT_GENERATION)

        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            restarted.wait_until_ready()
            wait_for_selected_generation(paths, RECOVERY_GENERATION)
            assert (
                saved_generation(paths, MANAGED_ACCOUNT_ID)
                == RECOVERY_GENERATION
            )
            assert saved_generation(paths, ACCOUNT_A_ID) == GENERATION
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()

        observer_a.close()
        observer_b.close()
    assert fixture.native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_codex_activation_commits_only_correlated_target(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = broker_fixture(tmp_path, short_socket_root, monkeypatch)
    selected = SelectedStateStore(fixture.paths.selected_state)
    selected.save(
        selected_account(
            ACCOUNT_A_ID,
            ACCOUNT_A_PROVIDER_IDENTITY,
            GENERATION,
        )
    )
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
            mode = fixture.provider_root / "mode"
            mode.write_text("malformed", encoding="utf-8")
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
            assert MANAGED_ACCOUNT_ID not in daemon.installed_account_ids
            _require_selected(
                selected,
                ACCOUNT_A_ID,
                ACCOUNT_A_PROVIDER_IDENTITY,
                GENERATION,
            )
            assert selected.load(ProviderId.CLAUDE) == claude

            mode.write_text("normal", encoding="utf-8")
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            completed = tuple(
                client.activate(ProviderId.CODEX, MANAGED_ACCOUNT_ID)
            )
            client.close()

            assert [event.kind for event in completed] == [
                EventKind.ACCEPTED,
                EventKind.PROGRESS,
                EventKind.COMPLETED,
            ]
            accepted, progress, terminal = (
                event.payload for event in completed
            )
            assert isinstance(accepted, AcceptedPayload)
            assert isinstance(progress, ProgressPayload)
            assert isinstance(terminal, CompletedPayload)
            assert (
                accepted.operation_id
                == progress.operation_id
                == terminal.operation_id
            )
            assert PROVIDER_IDENTITY not in repr(completed)
            assert RAW_PROVIDER_SECRET not in repr(completed)
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


def test_callback_preempts_stubborn_same_home_maintenance(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubborn same-home worker cannot consume the callback deadline."""
    fixture = broker_fixture(tmp_path, short_socket_root, monkeypatch)
    paths = fixture.paths
    route = write_worker_router(
        fixture.provider_root,
        _MAINTENANCE_OPERATION_ID,
        real_worker_executable(),
    )
    queue = OperationQueueStore(paths.durable_operations)

    with FakeCodexDaemon(fixture.native_home) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.native_home,
            daemon.socket_path,
        )
        observer_a = daemon.connect_tui()
        observer_b = daemon.connect_tui()
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            route.executable,
        ) as supervisor:
            supervisor.wait_until_ready()
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()
            queue.enqueue(
                DueOperation(
                    operation_id=_MAINTENANCE_OPERATION_ID,
                    provider_id=ProviderId.CODEX,
                    account_id=MANAGED_ACCOUNT_ID,
                    kind=OperationKind.MAINTAIN,
                    priority=OperationPriority.SCHEDULED,
                    state=OperationState.SCHEDULED,
                    due_at=REFERENCE_TIME,
                    updated_at=REFERENCE_TIME,
                )
            )
            supervisor.notify()
            wait_for_operation_state(
                queue,
                _MAINTENANCE_OPERATION_ID,
                OperationState.RUNNING,
            )
            wait_for_file(route.started)

            started = time.monotonic()
            refreshed = daemon.request_refresh(PROVIDER_IDENTITY)
            elapsed = time.monotonic() - started
            wait_for_selected_generation(paths, NEXT_GENERATION)
            maintenance = wait_for_operation_state(
                queue,
                _MAINTENANCE_OPERATION_ID,
                OperationState.RETRY_WAIT,
            )

            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == PROVIDER_IDENTITY
            assert elapsed < _CALLBACK_RESPONSE_BOUND_SECONDS
            assert maintenance.failure_code == "worker_preempted"
            process_id = int(route.started.read_text(encoding="utf-8"))
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)

        observer_a.close()
        observer_b.close()
