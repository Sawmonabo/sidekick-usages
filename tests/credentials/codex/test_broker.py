"""Load-bearing tests for the versioned managed Codex runtime."""

import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    FinalizedSelection,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    SelectionCode,
    SelectionOutcome,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import ControlEvent
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.auth.storage import (
    CODEX_AUTH_FILE,
    observe_native_auth,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_ERROR_CODE,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexLoadedThreadSnapshot,
    CodexSessionConfigurationReason,
)
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
    write_fake_managed_codex,
    write_resident_session_config,
    write_worker_router,
)
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.broker.runtime import (
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
    activation_source_fixture,
    broker_finalized_fixture,
    projection_matches_account,
    real_worker_executable,
    saved_generation,
    stage_provider_ahead,
    wait_for_file,
    wait_for_operation_state,
    wait_for_projected_generation,
)
from tests.fakes.codex.broker.supervisor import (
    FakeCodexBroker,
    FakeCodexSupervisor,
)
from tests.fakes.codex.managed import (
    managed_generation,
    managed_saved_account,
    seed_managed_accounts,
)
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME, FixedClock

pytestmark = REQUIRES_MANAGED_RUNTIME

_MAINTENANCE_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
_IDLE_BROKER_OBSERVATION_SECONDS = 0.6
_TERMINAL_FAILURE_OBSERVATION_SECONDS = 1.5
_CALLBACK_RESPONSE_BOUND_SECONDS = 8.0
_INITIAL_LIFECYCLE_CALLS = 2
_RECOVERED_LIFECYCLE_CALLS = 3
_INITIAL_READY_READS = 1
_REHYDRATED_READY_READS = 2
_GATED_ACTIVATION_ID = OperationId("99999999-9999-4999-8999-999999999999")


def _finalized_codex_selection(
    paths: ApplicationPaths,
) -> FinalizedSelection:
    """Return the required finalized Codex fixture pointer."""
    finalized = SelectedStateStore(paths.selected_state).load(ProviderId.CODEX)
    assert finalized is not None
    return finalized


def _select_codex_account(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> tuple[ControlEvent, ...]:
    """Collect one full control-plane selection result."""
    client = ControlClient.connect(
        paths.supervisor_socket,
        action_timeout_seconds=15.0,
    )
    try:
        client.handshake()
        return tuple(client.select_account(ProviderId.CODEX, account_id))
    finally:
        client.close()


def _assert_callback_rejection(
    supervisor: FakeCodexSupervisor,
    daemon: FakeCodexDaemon,
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
    provider_identity: str,
    schedule: Callable[[], None],
) -> None:
    """Require one real callback rejection before protected mutation."""
    supervisor.wait_until_ready()
    generation_before = saved_generation(paths, account_id)
    installed_before = daemon.installed_account_ids
    schedule()
    started = time.monotonic()
    stale = daemon.request_refresh(provider_identity)
    elapsed = time.monotonic() - started
    assert (stale.responder, stale.error_code) == (
        "sidekick_usages",
        CODEX_REFRESH_ERROR_CODE,
    )
    assert elapsed < _CALLBACK_RESPONSE_BOUND_SECONDS
    assert daemon.read_current_external_auth() == ProviderIdentity(
        provider_identity
    )
    assert daemon.installed_account_ids == installed_before
    assert saved_generation(paths, account_id) == generation_before
    supervisor.wait_until_callback_workers_collected()


def _finalize_projected_generation(
    paths: ApplicationPaths,
    generation: str,
) -> FinalizedSelection:
    """Emulate Task 4 finalization after exact broker projection proof."""
    selected = SelectedStateStore(paths.selected_state)
    initial = _finalized_codex_selection(paths)
    with ProviderMutationLock(
        paths.durable_operations,
        ProviderId.CODEX,
        (initial.account_id,),
        timeout_seconds=1.0,
    ).hold():
        assert selected.load(ProviderId.CODEX) == initial
        finalized = replace(
            initial,
            epoch=initial.epoch.next(),
            generation=AuthorityGeneration(generation),
        )
        selected.compare_and_swap(finalized, expected=initial)
    return finalized


def _persist_broker_gate(
    paths: ApplicationPaths,
    native_home: Path,
    gate: str,
) -> None:
    """Persist one exact reason the broker cannot resolve readiness."""
    native = observe_native_auth(
        credential_home=native_home,
        observed_at=REFERENCE_TIME,
    )
    RuntimeAuthObservationStore(paths.durable_operations).save_native(native)
    if gate == "unresolvable_selection":
        selected = SelectedStateStore(paths.selected_state)
        current = _finalized_codex_selection(paths)
        selected.compare_and_swap(
            replace(
                current,
                epoch=current.epoch.next(),
                generation=AuthorityGeneration(UNKNOWN_GENERATION),
            ),
            expected=current,
        )
        return
    journals = ActivationJournalStore(
        paths.activation_journals,
        paths.durable_operations,
    )
    with ProviderMutationLock(
        paths.durable_operations,
        ProviderId.CODEX,
        (MANAGED_ACCOUNT_ID,),
        timeout_seconds=1.0,
    ).hold() as authority:
        journals.transaction(
            ProviderId.CODEX,
            (MANAGED_ACCOUNT_ID,),
            authority,
        ).begin(
            ActivationRecord(
                provider_id=ProviderId.CODEX,
                operation_id=_GATED_ACTIVATION_ID,
                selected_baseline=None,
                native_auth_baseline=native,
                target_account_id=MANAGED_ACCOUNT_ID,
                expected_target_identity=ProviderIdentity(PROVIDER_IDENTITY),
                target_authority_generation=AuthorityGeneration(GENERATION),
                phase=ActivationPhase.PREPARED,
                started_at=REFERENCE_TIME,
                updated_at=REFERENCE_TIME,
            )
        )


@pytest.mark.parametrize(
    "gate",
    ["active_transition", "unresolvable_selection"],
)
def test_resident_broker_fails_closed_without_resolvable_authority(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    """Durable transition or missing authority proof keeps admission gated."""
    fixture = broker_finalized_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    paths = fixture.paths
    _persist_broker_gate(paths, fixture.native_home, gate)

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        with FakeCodexBroker(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
        ) as broker:
            broker.wait_until_available()
            time.sleep(_IDLE_BROKER_OBSERVATION_SECONDS)
            assert (
                broker.available,
                broker.ready,
                broker.failure_code,
                daemon.installed_account_ids,
            ) == (True, False, None, ())


def test_stale_resident_config_is_terminal_until_operator_restart(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = broker_finalized_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    write_resident_session_config(
        fixture.session_home,
        model_provider="stale-resident-provider",
    )

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
            already_running=True,
        )
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_broker_failure(
                CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED.value
            )
            report = supervisor.broker_preparation_report
            assert report is not None
            assert report.reason == (
                CodexSessionConfigurationReason.RESIDENT_CONFIG_STALE.value
            )
            assert report.dry_run is True
            assert (
                report.operator_steps[0] == CODEX_SESSION_OPERATOR_PRECONDITION
            )
            observed = (
                lifecycle.start_statuses,
                lifecycle.version_count,
                lifecycle.restart_count,
            )
            time.sleep(_TERMINAL_FAILURE_OBSERVATION_SECONDS)
            assert supervisor.broker_failure_code == (
                CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED.value
            )
            assert supervisor.broker_preparation_report == report
            assert (
                lifecycle.start_statuses,
                lifecycle.version_count,
                lifecycle.restart_count,
            ) == observed

    assert observed == (("alreadyRunning",), 1, 0)


def test_shared_codex_runtime_is_idempotent_and_rehydrates(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    schema_root = tmp_path / "schema"
    native_home = short_socket_root / "native-authority"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(NATIVE_AUTH_SENTINEL)
    os.chmod(native_auth, 0o600)
    session_home = short_socket_root / "session"
    session_home.mkdir()
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(
        tmp_path,
        schema_root,
        session_home,
        version="0.146.0",
    )
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

    with FakeCodexDaemon(
        session_home,
        app_server_version="0.146.0",
    ) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            tmp_path,
            session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        runtime = CodexSharedRuntime.create(
            executable,
            session_home,
            environment=environment,
            loaded_threads=lambda: CodexLoadedThreadSnapshot(
                revision=0,
                thread_ids=(),
            ),
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
    assert not (session_home / "auth.json").exists()


def test_resident_broker_refreshes_and_recovers_provider_ahead_state(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resident broker refreshes B and repairs a provider-ahead restart."""
    fixture = broker_finalized_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    paths = fixture.paths
    finalized_before = _finalized_codex_selection(paths)

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.144.0",
            cli_version="0.146.0",
        )
        observer_a = daemon.connect_tui()
        observer_b = daemon.connect_tui()
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_broker_failure("version_unsupported")
            assert not supervisor.broker_available
            configure_codex_daemon_lifecycle(
                fixture.provider_root,
                fixture.session_home,
                daemon.socket_path,
                app_server_version="0.146.0",
                managed=False,
            )
            supervisor.wait_until_ready()
            assert (
                supervisor.broker_available,
                supervisor.broker_failure_code,
            ) == (True, None)
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()
            dashboard = ControlClient.connect(paths.supervisor_socket)
            try:
                dashboard.handshake()
            finally:
                dashboard.close()

            rejected = daemon.request_refresh("unknown-provider-account")
            assert (rejected.responder, rejected.error_code) == (
                "sidekick_usages",
                CODEX_REFRESH_ERROR_CODE,
            )
            refreshed = daemon.request_refresh(PROVIDER_IDENTITY)
            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == PROVIDER_IDENTITY
            wait_for_projected_generation(
                paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            assert (
                saved_generation(paths, MANAGED_ACCOUNT_ID),
                _finalized_codex_selection(paths),
            ) == (NEXT_GENERATION, finalized_before)
            finalized_next = _finalize_projected_generation(
                paths,
                NEXT_GENERATION,
            )
            assert saved_generation(paths, ACCOUNT_A_ID) == GENERATION
            initial_starts = lifecycle.start_statuses
            initial_versions = lifecycle.version_count
            time.sleep(_IDLE_BROKER_OBSERVATION_SECONDS)
            assert lifecycle.start_statuses == initial_starts
            assert lifecycle.version_count == initial_versions

        stage_provider_ahead(fixture)
        assert (
            managed_generation(fixture.private, MANAGED_ACCOUNT_ID),
            saved_generation(paths, MANAGED_ACCOUNT_ID),
            projection_matches_account(
                paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
            ),
            _finalized_codex_selection(paths),
        ) == (
            RECOVERY_GENERATION,
            NEXT_GENERATION,
            False,
            finalized_next,
        )

        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            wait_for_projected_generation(
                paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                RECOVERY_GENERATION,
            )
            assert (
                saved_generation(paths, MANAGED_ACCOUNT_ID),
                _finalized_codex_selection(paths),
            ) == (RECOVERY_GENERATION, finalized_next)
            finalized_recovery = _finalize_projected_generation(
                paths,
                RECOVERY_GENERATION,
            )
            restarted.wait_until_ready()
            assert (
                _finalized_codex_selection(paths),
                set(daemon.installed_account_ids),
            ) == (finalized_recovery, {PROVIDER_IDENTITY})
            assert saved_generation(paths, ACCOUNT_A_ID) == GENERATION
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()

        observer_a.close()
        observer_b.close()
    assert fixture.native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_selection_worker_binds_codex_broker_journey(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex selection uses one epoch-bound resident broker journey."""
    fixture = activation_source_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
        account_a_private_generation=GENERATION,
    )
    paths = fixture.paths
    initial = _finalized_codex_selection(paths)

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        observer = daemon.connect_tui()
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            observer.wait_for_account_update()
            assert (
                _finalized_codex_selection(paths),
                set(daemon.installed_account_ids),
                daemon.mcp_status_thread_ids,
            ) == (
                initial,
                {ACCOUNT_A_PROVIDER_IDENTITY},
                ("thread-alpha", "thread-alpha"),
            )

            assert saved_generation(paths, MANAGED_ACCOUNT_ID) == GENERATION
            supervisor.schedule_selection_hook(
                OperationKind.SELECTION_COMMIT,
                daemon.replace_socket_listener,
            )
            target_auth = (
                managed_codex_home(paths, MANAGED_ACCOUNT_ID) / CODEX_AUTH_FILE
            )
            supervisor.schedule_selection_hook(
                OperationKind.SELECTION_READBACK,
                target_auth.unlink,
            )
            rejected_events = _select_codex_account(
                paths,
                MANAGED_ACCOUNT_ID,
            )
            rejected = rejected_events[-1].payload
            assert isinstance(rejected, SelectionResult), rejected_events
            assert (rejected.outcome, rejected.safe_code) == (
                SelectionOutcome.RECOVERY_REQUIRED,
                SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )
            supervisor.wait_until_selection_workers_collected()
            recovered = SelectionOperationStore(paths.selection_journals).load(
                ProviderId.CODEX
            )
            assert (
                _finalized_codex_selection(paths),
                set(daemon.installed_account_ids),
                saved_generation(paths, MANAGED_ACCOUNT_ID),
                recovered.active,
                recovered.history[-1].outcome,
            ) == (
                initial,
                {ACCOUNT_A_PROVIDER_IDENTITY},
                NEXT_GENERATION,
                None,
                SelectionOutcome.FAILED_OLD_EPOCH,
            )
            _assert_callback_rejection(
                supervisor,
                daemon,
                paths,
                ACCOUNT_A_ID,
                ACCOUNT_A_PROVIDER_IDENTITY,
                supervisor.schedule_stale_callback_epoch,
            )
            _assert_callback_rejection(
                supervisor,
                daemon,
                paths,
                ACCOUNT_A_ID,
                ACCOUNT_A_PROVIDER_IDENTITY,
                supervisor.schedule_stale_callback_request,
            )
            _assert_callback_rejection(
                supervisor,
                daemon,
                paths,
                ACCOUNT_A_ID,
                ACCOUNT_A_PROVIDER_IDENTITY,
                lambda: supervisor.schedule_wrong_callback_home(
                    MANAGED_ACCOUNT_ID
                ),
            )

        observer.close()
    assert fixture.native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_callback_preempts_stubborn_same_home_maintenance(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubborn same-home worker cannot consume the callback deadline."""
    fixture = broker_finalized_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    paths = fixture.paths
    finalized_before = _finalized_codex_selection(paths)
    route = write_worker_router(
        fixture.provider_root,
        _MAINTENANCE_OPERATION_ID,
        real_worker_executable(),
    )
    queue = OperationQueueStore(paths.durable_operations)

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        observer_a = daemon.connect_tui()
        observer_b = daemon.connect_tui()
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
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
            wait_for_projected_generation(
                paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            maintenance = wait_for_operation_state(
                queue,
                _MAINTENANCE_OPERATION_ID,
                OperationState.RETRY_WAIT,
            )

            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == PROVIDER_IDENTITY
            assert elapsed < _CALLBACK_RESPONSE_BOUND_SECONDS
            assert maintenance.failure_code == "worker_preempted"
            assert _finalized_codex_selection(paths) == finalized_before
            process_id = int(route.started.read_text(encoding="utf-8"))
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)

        observer_a.close()
        observer_b.close()
