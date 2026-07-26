"""Load-bearing tests for the versioned managed Codex runtime."""

import os
import time
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    OperationId,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
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
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
    write_fake_managed_codex,
    write_worker_router,
)
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.broker.runtime import (
    ACCOUNT_A_ID,
    GENERATION,
    MANAGED_ACCOUNT_ID,
    MANAGED_AUTHORITY_ID,
    NATIVE_AUTH_SENTINEL,
    NEXT_GENERATION,
    PROVIDER_IDENTITY,
    RECOVERY_GENERATION,
    broker_fixture,
    real_worker_executable,
    saved_generation,
    stage_provider_ahead,
    wait_for_file,
    wait_for_operation_state,
    wait_for_selected_generation,
)
from tests.fakes.codex.broker.supervisor import FakeCodexSupervisor
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
_CALLBACK_RESPONSE_BOUND_SECONDS = 8.0
_INITIAL_LIFECYCLE_CALLS = 2
_RECOVERED_LIFECYCLE_CALLS = 3
_INITIAL_READY_READS = 1
_REHYDRATED_READY_READS = 2


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
            app_server_version="0.144.0",
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
            supervisor.wait_until_broker_failure("version_unsupported")
            assert not supervisor.broker_available
            configure_codex_daemon_lifecycle(
                fixture.provider_root,
                fixture.native_home,
                daemon.socket_path,
            )
            supervisor.wait_until_ready()
            assert supervisor.broker_available
            assert supervisor.broker_failure_code is None
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
