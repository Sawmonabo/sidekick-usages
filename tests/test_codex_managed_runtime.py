"""Load-bearing tests for the versioned managed Codex runtime."""

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
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
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.paths import (
    ApplicationPaths,
    discover_application_paths,
    managed_codex_home,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcNotification,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_ERROR_CODE,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from tests.fakes.codex.auth import NEXT_AUTH_FILE, managed_auth
from tests.fakes.codex.daemon import FakeCodexDaemon
from tests.fakes.codex.executable import (
    RAW_PROVIDER_SECRET,
    configure_codex_daemon_lifecycle,
    write_fake_codex,
    write_fake_managed_codex,
    write_worker_router,
)
from tests.fakes.codex.managed import (
    managed_generation,
    managed_saved_account,
    managed_subscription,
    seed_managed_accounts,
)
from tests.fakes.codex.models import FakeCodexBrokerFixture
from tests.fakes.codex.schema import write_codex_schema
from tests.fakes.codex.supervisor import FakeCodexSupervisor
from tests.test_support import REFERENCE_TIME, FixedClock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Managed Codex runtimes require Linux, WSL, or macOS.",
)

SCHEMA_HASH_HEX_LENGTH = 64
_ACCOUNT_A_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_A_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_ACCOUNT_A_PROVIDER_IDENTITY = "workspace-account-unselected"
_MANAGED_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_MANAGED_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NEXT_GENERATION = "2026-07-24T10:01:00.000000000Z"
_RECOVERY_GENERATION = "2026-07-24T10:02:00.000000000Z"
_UNSELECTED_NEXT_GENERATION = "2026-07-24T10:03:00.000000000Z"
_NATIVE_AUTH_SENTINEL = b'{"native":"must-remain-unchanged"}\n'
_MAINTENANCE_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
_WAIT_TIMEOUT_SECONDS = 10.0
_WAIT_INTERVAL_SECONDS = 0.01
_IDLE_BROKER_OBSERVATION_SECONDS = 0.6
_CALLBACK_RESPONSE_BOUND_SECONDS = 8.0
_INITIAL_LIFECYCLE_CALLS = 2
_RECOVERED_LIFECYCLE_CALLS = 3
_INITIAL_READY_READS = 1
_REHYDRATED_READY_READS = 2


@pytest.fixture
def short_socket_root() -> Iterator[Path]:
    """Provide a native home below the Unix socket path-length limit."""
    with tempfile.TemporaryDirectory(prefix="sku-") as root:
        yield Path(root)


def _prepare_shared_runtime(
    executable: CodexExecutable,
    native_home: Path,
    environment: dict[str, str],
    expected_user_id: int | None,
) -> None:
    runtime = CodexSharedRuntime.create(
        executable,
        native_home,
        environment=environment,
        expected_user_id=expected_user_id,
    )
    runtime.prepare(
        _MANAGED_ACCOUNT_ID,
        ProviderIdentity(_PROVIDER_IDENTITY),
        AuthorityGeneration(_GENERATION),
    )


def _isolated_runtime(
    root: Path,
    provider_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ApplicationPaths, dict[str, str]]:
    root.mkdir()
    home = root / "home"
    data = root / "data"
    runtime = root / "runtime"
    home.mkdir()
    data.mkdir()
    runtime.mkdir(mode=0o700)
    environment = {
        "HOME": str(home),
        "PATH": os.pathsep.join((str(provider_root), os.environ["PATH"])),
        "XDG_DATA_HOME": str(data),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return discover_application_paths(), environment


def _selected_account(
    account_id: SidekickAccountId,
    provider_identity: str,
    generation: str,
) -> SelectedAccountState:
    return SelectedAccountState(
        provider_id=ProviderId.CODEX,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        account_id=account_id,
        provider_identity=ProviderIdentity(provider_identity),
        runtime_generation=AuthorityGeneration(generation),
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.VERIFIED,
    )


def _broker_fixture(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeCodexBrokerFixture:
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    schema_root = provider_root / "schema"
    native_home = short_socket_root / "native"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(_NATIVE_AUTH_SENTINEL)
    os.chmod(native_auth, 0o600)
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(provider_root, schema_root, native_home)
    paths, environment = _isolated_runtime(
        short_socket_root / "state",
        provider_root,
        monkeypatch,
    )
    account_a = managed_saved_account(
        _ACCOUNT_A_ID,
        _ACCOUNT_A_AUTHORITY_ID,
        "codex-unselected",
        _ACCOUNT_A_PROVIDER_IDENTITY,
        _GENERATION,
    )
    account_b = managed_saved_account(
        _MANAGED_ACCOUNT_ID,
        _MANAGED_AUTHORITY_ID,
        "codex-selected",
        _PROVIDER_IDENTITY,
        _GENERATION,
    )
    seeded_paths, _store, private = seed_managed_accounts(
        paths.accounts.parent,
        (account_a, account_b),
        {
            _ACCOUNT_A_ID: managed_auth(
                _ACCOUNT_A_PROVIDER_IDENTITY,
                _UNSELECTED_NEXT_GENERATION,
            ),
            _MANAGED_ACCOUNT_ID: managed_auth(
                _PROVIDER_IDENTITY,
                _NEXT_GENERATION,
            ),
        },
    )
    if seeded_paths.accounts != paths.accounts:
        raise AssertionError("Discovered and synthetic paths disagree.")
    SelectedStateStore(paths.selected_state).save(
        _selected_account(
            _MANAGED_ACCOUNT_ID,
            _PROVIDER_IDENTITY,
            _GENERATION,
        )
    )
    return FakeCodexBrokerFixture(
        paths,
        environment,
        discover_codex_executable(environment),
        private,
        provider_root,
        native_home,
        native_auth,
    )


def _account_store(paths: ApplicationPaths) -> AccountStore:
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return AccountStore(paths.accounts, private).load()


def _saved_generation(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> str:
    account = _account_store(paths).read_saved(account_id)
    if account is None:
        raise AssertionError("Managed Codex account disappeared.")
    return str(managed_subscription(account).generation)


def _wait_for_selected_generation(
    paths: ApplicationPaths,
    generation: str,
) -> None:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    selected = SelectedStateStore(paths.selected_state)
    while True:
        state = selected.load(ProviderId.CODEX)
        if (
            state is not None
            and state.runtime_generation == AuthorityGeneration(generation)
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Selected Codex generation did not advance.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def _wait_for_operation_state(
    queue: OperationQueueStore,
    operation_id: OperationId,
    state: OperationState,
) -> DueOperation:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while True:
        operation = queue.find(operation_id)
        if operation is not None and operation.state is state:
            return operation
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Durable operation did not reach its state.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while not path.is_file():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Expected worker marker was not created.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def _real_worker_executable() -> Path:
    executable = Path(sys.executable).parent / "sidekick-usages-worker"
    try:
        resolved = executable.resolve(strict=True)
    except OSError:
        raise AssertionError(
            "Editable worker entrypoint is unavailable."
        ) from None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AssertionError("Editable worker entrypoint is unsafe.")
    return resolved


def _stage_provider_ahead(
    fixture: FakeCodexBrokerFixture,
) -> None:
    paths = fixture.paths
    next_auth = managed_codex_home(paths, _MANAGED_ACCOUNT_ID) / NEXT_AUTH_FILE
    next_auth.write_bytes(
        managed_auth(_PROVIDER_IDENTITY, _RECOVERY_GENERATION)
    )
    os.chmod(next_auth, 0o600)
    coordinator = CodexManagedAuthorityCoordinator(
        paths,
        _account_store(paths),
        fixture.private,
        probe_codex_capabilities(
            fixture.executable,
            fixture.environment,
        ),
        FixedClock(),
        environment=fixture.environment,
    )
    lock = OperationAuthorityLock(
        paths.durable_operations,
        _MANAGED_ACCOUNT_ID,
    )
    with lock.hold() as authority:
        staged = coordinator.stage_refresh_with_authority(
            _MANAGED_ACCOUNT_ID,
            authority,
            CodexProjectionExpectation(
                _MANAGED_ACCOUNT_ID,
                ProviderIdentity(_PROVIDER_IDENTITY),
                AuthorityGeneration(_NEXT_GENERATION),
            ),
        )
    if isinstance(staged, CodexManagedAuthorityResult):
        raise AssertionError("Synthetic provider-ahead refresh failed.")


def test_versioned_codex_app_server_boundary_is_complete(
    tmp_path: Path,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    executable_path = write_fake_codex(tmp_path, schema_root)
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }

    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    with CodexAppServerSession.open(
        capabilities,
        codex_home,
        environment,
    ) as session:
        result = session.request(
            "account/read",
            {"refreshToken": False},
        )
        notification = session.receive()

        assert executable.path == executable_path.resolve()
        assert str(executable.version) == "0.145.0"
        assert len(capabilities.schema_hash) == SCHEMA_HASH_HEX_LENGTH
        assert result["requiresOpenaiAuth"] is True
        assert isinstance(notification, JsonRpcNotification)
        assert notification.method == "account/updated"
    assert session.closed

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert events[0]["argv"] == ["--version"]
    assert events[1]["argv"][:4] == [
        "app-server",
        "generate-json-schema",
        "--experimental",
        "--out",
    ]
    assert Path(events[1]["argv"][4]).name == "schema"
    assert not Path(events[1]["argv"][4]).exists()
    assert events[2]["argv"] == ["app-server"]
    assert all(event.get("openai_api_key") is None for event in events)
    assert events[-1]["codex_home"] == str(codex_home)


def test_codex_app_server_boundary_fails_closed_and_redacted(
    tmp_path: Path,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(tmp_path, schema_root)
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    (tmp_path / "mode").write_text("malformed", encoding="utf-8")

    with pytest.raises(CodexAppServerError) as malformed:
        CodexAppServerSession.open(
            capabilities,
            codex_home,
            environment,
        )

    assert malformed.value.code is CodexAppServerFailure.PROTOCOL_MALFORMED
    assert RAW_PROVIDER_SECRET not in repr(malformed.value)
    process_id = int((tmp_path / "app-server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


def test_shared_codex_runtime_is_idempotent_and_rehydrates(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    schema_root = tmp_path / "schema"
    native_home = short_socket_root / "native"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(_NATIVE_AUTH_SENTINEL)
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
        _MANAGED_ACCOUNT_ID,
        _MANAGED_AUTHORITY_ID,
        "codex-alpha",
        _PROVIDER_IDENTITY,
        _GENERATION,
    )
    paths, store, private = seed_managed_accounts(
        tmp_path,
        (account,),
        {
            _MANAGED_ACCOUNT_ID: managed_auth(
                _PROVIDER_IDENTITY,
                _NEXT_GENERATION,
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
            _MANAGED_ACCOUNT_ID,
            runtime,
        )
        second = coordinator.install_current_projection(
            _MANAGED_ACCOUNT_ID,
            runtime,
        )

        assert second == first
        assert runtime.ready
        observer_a.wait_for_account_update()
        observer_b.wait_for_account_update()
        assert daemon.installed_account_ids == (_PROVIDER_IDENTITY,)
        assert daemon.ready_account_read_count == _INITIAL_READY_READS
        assert lifecycle.start_statuses == ("started", "alreadyRunning")
        assert lifecycle.version_count == _INITIAL_LIFECYCLE_CALLS

        daemon.replace()
        assert not runtime.ready
        observer_after_replacement = daemon.connect_tui()
        coordinator.install_current_projection(
            _MANAGED_ACCOUNT_ID,
            runtime,
        )

        assert runtime.ready
        observer_after_replacement.wait_for_account_update()
        assert daemon.installed_account_ids == (
            _PROVIDER_IDENTITY,
            _PROVIDER_IDENTITY,
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

    assert native_auth.read_bytes() == _NATIVE_AUTH_SENTINEL


def test_resident_broker_refreshes_and_recovers_provider_ahead_state(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resident broker refreshes B and repairs a provider-ahead restart."""
    fixture = _broker_fixture(tmp_path, short_socket_root, monkeypatch)
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
            _real_worker_executable(),
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
            refreshed = daemon.request_refresh(_PROVIDER_IDENTITY)
            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == _PROVIDER_IDENTITY
            _wait_for_selected_generation(paths, _NEXT_GENERATION)
            assert (
                _saved_generation(paths, _MANAGED_ACCOUNT_ID)
                == _NEXT_GENERATION
            )
            assert _saved_generation(paths, _ACCOUNT_A_ID) == _GENERATION
            initial_starts = lifecycle.start_statuses
            initial_versions = lifecycle.version_count
            time.sleep(_IDLE_BROKER_OBSERVATION_SECONDS)
            assert lifecycle.start_statuses == initial_starts
            assert lifecycle.version_count == initial_versions

        _stage_provider_ahead(fixture)
        assert (
            managed_generation(fixture.private, _MANAGED_ACCOUNT_ID)
            == _RECOVERY_GENERATION
        )
        assert (
            _saved_generation(paths, _MANAGED_ACCOUNT_ID) == _NEXT_GENERATION
        )
        _wait_for_selected_generation(paths, _NEXT_GENERATION)

        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            _real_worker_executable(),
        ) as restarted:
            restarted.wait_until_ready()
            _wait_for_selected_generation(paths, _RECOVERY_GENERATION)
            assert (
                _saved_generation(paths, _MANAGED_ACCOUNT_ID)
                == _RECOVERY_GENERATION
            )
            assert _saved_generation(paths, _ACCOUNT_A_ID) == _GENERATION
            observer_a.wait_for_account_update()
            observer_b.wait_for_account_update()

        observer_a.close()
        observer_b.close()

    assert fixture.native_auth.read_bytes() == _NATIVE_AUTH_SENTINEL


def test_callback_preempts_stubborn_same_home_maintenance(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubborn same-home worker cannot consume the callback deadline."""
    fixture = _broker_fixture(tmp_path, short_socket_root, monkeypatch)
    paths = fixture.paths
    route = write_worker_router(
        fixture.provider_root,
        _MAINTENANCE_OPERATION_ID,
        _real_worker_executable(),
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
                    account_id=_MANAGED_ACCOUNT_ID,
                    kind=OperationKind.MAINTAIN,
                    priority=OperationPriority.SCHEDULED,
                    state=OperationState.SCHEDULED,
                    due_at=REFERENCE_TIME,
                    updated_at=REFERENCE_TIME,
                )
            )
            supervisor.notify()
            _wait_for_operation_state(
                queue,
                _MAINTENANCE_OPERATION_ID,
                OperationState.RUNNING,
            )
            _wait_for_file(route.started)

            started = time.monotonic()
            refreshed = daemon.request_refresh(_PROVIDER_IDENTITY)
            elapsed = time.monotonic() - started
            _wait_for_selected_generation(paths, _NEXT_GENERATION)
            maintenance = _wait_for_operation_state(
                queue,
                _MAINTENANCE_OPERATION_ID,
                OperationState.RETRY_WAIT,
            )

            assert refreshed.responder == "sidekick_usages"
            assert refreshed.account_id == _PROVIDER_IDENTITY
            assert elapsed < _CALLBACK_RESPONSE_BOUND_SECONDS
            assert maintenance.failure_code == "worker_preempted"
            process_id = int(route.started.read_text(encoding="utf-8"))
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)

        observer_a.close()
        observer_b.close()


def test_shared_codex_runtime_rejects_each_preflight_authority(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    cases = (
        (
            "version",
            True,
            "0.146.0",
            None,
            CodexBrokerFailure.VERSION_UNSUPPORTED,
        ),
        (
            "schema",
            False,
            "0.145.0",
            None,
            CodexBrokerFailure.PROTOCOL_UNSUPPORTED,
        ),
        (
            "owner",
            True,
            "0.145.0",
            os.geteuid() + 1,
            CodexBrokerFailure.RUNTIME_UNSAFE,
        ),
    )
    for (
        name,
        external_auth,
        daemon_version,
        expected_user_id,
        expected_failure,
    ) in cases:
        root = tmp_path / name
        root.mkdir()
        schema_root = root / "schema"
        native_home = short_socket_root / name
        native_home.mkdir()
        native_auth = native_home / "auth.json"
        native_auth.write_bytes(_NATIVE_AUTH_SENTINEL)
        os.chmod(native_auth, 0o600)
        write_codex_schema(schema_root, external_auth=external_auth)
        write_fake_managed_codex(root, schema_root, native_home)
        environment = {
            "HOME": str(root),
            "PATH": os.pathsep.join((str(root), os.environ["PATH"])),
        }
        executable = discover_codex_executable(environment)

        with FakeCodexDaemon(
            native_home,
            app_server_version=daemon_version,
        ) as daemon:
            configure_codex_daemon_lifecycle(
                root,
                native_home,
                daemon.socket_path,
                app_server_version=daemon_version,
            )
            with pytest.raises(CodexBrokerError) as rejected:
                _prepare_shared_runtime(
                    executable,
                    native_home,
                    environment,
                    expected_user_id,
                )

            assert rejected.value.code is expected_failure
            assert daemon.installed_account_ids == ()
            assert native_auth.read_bytes() == _NATIVE_AUTH_SENTINEL
