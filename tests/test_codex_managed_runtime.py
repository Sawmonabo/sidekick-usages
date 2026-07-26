"""Load-bearing tests for the versioned managed Codex runtime."""

import os
import sys
import time
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
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    FailedPayload,
    ProgressPayload,
)
from sidekick_usages.daemon.types.protocol import EventKind
from sidekick_usages.paths import (
    ApplicationPaths,
    discover_application_paths,
    managed_codex_home,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
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
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from tests.fakes.codex.auth import NEXT_AUTH_FILE, managed_auth
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

_ACCOUNT_A_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_A_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_ACCOUNT_A_PROVIDER_IDENTITY = "workspace-account-unselected"
_MANAGED_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_MANAGED_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_CLAUDE_ACCOUNT_ID = SidekickAccountId("55555555-5555-4555-8555-555555555555")
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NEXT_GENERATION = "2026-07-24T10:01:00.000000000Z"
_RECOVERY_GENERATION = "2026-07-24T10:02:00.000000000Z"
_UNSELECTED_NEXT_GENERATION = "2026-07-24T10:03:00.000000000Z"
_UNKNOWN_GENERATION = "2026-07-24T10:04:00.000000000Z"
_UNKNOWN_PROVIDER_IDENTITY = "workspace-account-external"
_NATIVE_AUTH_SENTINEL = managed_auth(
    _ACCOUNT_A_PROVIDER_IDENTITY,
    _GENERATION,
)
_MAINTENANCE_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
_FIRST_ACTIVATION_ID = OperationId("88888888-8888-4888-8888-888888888888")
_SECOND_ACTIVATION_ID = OperationId("99999999-9999-4999-8999-999999999999")
_WAIT_TIMEOUT_SECONDS = 10.0
_WAIT_INTERVAL_SECONDS = 0.01
_IDLE_BROKER_OBSERVATION_SECONDS = 0.6
_CALLBACK_RESPONSE_BOUND_SECONDS = 8.0
_INITIAL_LIFECYCLE_CALLS = 2
_RECOVERED_LIFECYCLE_CALLS = 3
_INITIAL_READY_READS = 1
_REHYDRATED_READY_READS = 2


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
    paths, environment = _isolated_runtime(
        short_socket_root / "state",
        provider_root,
        monkeypatch,
    )
    native_home = Path(environment["HOME"]) / ".codex"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(_NATIVE_AUTH_SENTINEL)
    os.chmod(native_auth, 0o600)
    (native_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n',
        encoding="utf-8",
    )
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(provider_root, schema_root, native_home)
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


def _wait_for_external_selection(
    paths: ApplicationPaths,
    provider_identity: str,
) -> SelectedAccountState:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    selected = SelectedStateStore(paths.selected_state)
    while True:
        state = selected.load(ProviderId.CODEX)
        if (
            state is not None
            and state.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE
            and state.provider_identity == ProviderIdentity(provider_identity)
        ):
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("External Codex selection was not related.")
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


def _enqueue_activation(
    paths: ApplicationPaths,
    operation_id: OperationId,
    account_id: SidekickAccountId,
) -> None:
    OperationQueueStore(paths.durable_operations).enqueue(
        DueOperation(
            operation_id=operation_id,
            provider_id=ProviderId.CODEX,
            account_id=account_id,
            kind=OperationKind.ACTIVATE,
            priority=OperationPriority.INTERACTIVE,
            state=OperationState.SCHEDULED,
            due_at=REFERENCE_TIME,
            updated_at=REFERENCE_TIME,
        )
    )


def _interrupt_activation_at_install(
    supervisor: FakeCodexSupervisor,
    daemon: FakeCodexDaemon,
    paths: ApplicationPaths,
    operation_id: OperationId,
    account_id: SidekickAccountId,
) -> None:
    daemon.pause_next_install()
    _enqueue_activation(paths, operation_id, account_id)
    supervisor.notify()
    daemon.wait_for_paused_install()
    supervisor.request_stop()
    _wait_for_operation_state(
        OperationQueueStore(paths.durable_operations),
        operation_id,
        OperationState.RETRY_WAIT,
    )
    daemon.resume_install()
    supervisor.close()


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


def test_codex_activation_commits_only_correlated_target(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _broker_fixture(tmp_path, short_socket_root, monkeypatch)
    selected = SelectedStateStore(fixture.paths.selected_state)
    selected.save(
        _selected_account(
            _ACCOUNT_A_ID,
            _ACCOUNT_A_PROVIDER_IDENTITY,
            _GENERATION,
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
            _real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            mode = fixture.provider_root / "mode"
            mode.write_text("malformed", encoding="utf-8")
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            failed = tuple(
                client.activate(ProviderId.CODEX, _MANAGED_ACCOUNT_ID)
            )
            client.close()

            assert [event.kind for event in failed] == [
                EventKind.ACCEPTED,
                EventKind.PROGRESS,
                EventKind.FAILED,
            ]
            assert isinstance(failed[-1].payload, FailedPayload)
            assert _MANAGED_ACCOUNT_ID not in daemon.installed_account_ids
            _require_selected(
                selected,
                _ACCOUNT_A_ID,
                _ACCOUNT_A_PROVIDER_IDENTITY,
                _GENERATION,
            )
            assert selected.load(ProviderId.CLAUDE) == claude

            mode.write_text("normal", encoding="utf-8")
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            completed = tuple(
                client.activate(ProviderId.CODEX, _MANAGED_ACCOUNT_ID)
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
            assert _PROVIDER_IDENTITY not in repr(completed)
            assert RAW_PROVIDER_SECRET not in repr(completed)
            _require_selected(
                selected,
                _MANAGED_ACCOUNT_ID,
                _PROVIDER_IDENTITY,
                _NEXT_GENERATION,
            )
            journal = ActivationJournalStore(
                fixture.paths.activation_journals,
                fixture.paths.durable_operations,
            ).load(ProviderId.CODEX)
            assert journal.active is None
            assert journal.history[-1].phase is ActivationPhase.COMMITTED
            assert journal.history[-1].target_authority_generation == (
                AuthorityGeneration(_NEXT_GENERATION)
            )
            assert journal.history[-1].verified_runtime_generation == (
                AuthorityGeneration(_NEXT_GENERATION)
            )
            assert selected.load(ProviderId.CLAUDE) == claude

    assert fixture.native_auth.read_bytes() == _NATIVE_AUTH_SENTINEL


def test_codex_activation_recovers_at_official_mutation_boundary(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _broker_fixture(tmp_path, short_socket_root, monkeypatch)
    selected = SelectedStateStore(fixture.paths.selected_state)
    selected.save(
        _selected_account(
            _ACCOUNT_A_ID,
            _ACCOUNT_A_PROVIDER_IDENTITY,
            _GENERATION,
        )
    )
    journals = ActivationJournalStore(
        fixture.paths.activation_journals,
        fixture.paths.durable_operations,
    )

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
            _real_worker_executable(),
        )
        supervisor.start()
        supervisor.wait_until_ready()
        _interrupt_activation_at_install(
            supervisor,
            daemon,
            fixture.paths,
            _FIRST_ACTIVATION_ID,
            _MANAGED_ACCOUNT_ID,
        )

        assert daemon.installed_account_ids[-1] == _PROVIDER_IDENTITY
        _require_selected(
            selected,
            _ACCOUNT_A_ID,
            _ACCOUNT_A_PROVIDER_IDENTITY,
            _GENERATION,
        )
        assert journals.load(ProviderId.CODEX).active is not None
        installed_before_recovery = len(daemon.installed_account_ids)

        daemon.pause_next_install()
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            _real_worker_executable(),
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

            assert (
                len(daemon.installed_account_ids) > installed_before_recovery
            )
            _require_selected(
                selected,
                _MANAGED_ACCOUNT_ID,
                _PROVIDER_IDENTITY,
                _NEXT_GENERATION,
            )
            recovered = journals.load(ProviderId.CODEX)
            assert recovered.active is None
            assert len(recovered.history) == 1

            _interrupt_activation_at_install(
                restarted,
                daemon,
                fixture.paths,
                _SECOND_ACTIVATION_ID,
                _ACCOUNT_A_ID,
            )
            account_a_installs = daemon.installed_account_ids.count(
                _ACCOUNT_A_PROVIDER_IDENTITY
            )
            account_b_installs = daemon.installed_account_ids.count(
                _PROVIDER_IDENTITY
            )

        daemon.perform_external_runtime_login(
            _PROVIDER_IDENTITY,
            _NEXT_GENERATION,
        )
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.native_home,
            fixture.environment,
            _real_worker_executable(),
        ) as external_recovery:
            external_recovery.wait_until_ready()
            assert (
                daemon.installed_account_ids.count(
                    _ACCOUNT_A_PROVIDER_IDENTITY
                )
                == account_a_installs
            )
            assert (
                daemon.installed_account_ids.count(_PROVIDER_IDENTITY)
                > account_b_installs
            )
            _require_selected(
                selected,
                _MANAGED_ACCOUNT_ID,
                _PROVIDER_IDENTITY,
                _NEXT_GENERATION,
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
                AuthorityGeneration(_UNSELECTED_NEXT_GENERATION),
                AuthorityGeneration(_NEXT_GENERATION),
            )
            saved_ids = tuple(
                account.account_id
                for account in _account_store(fixture.paths).saved_accounts()
            )
            daemon.perform_external_runtime_login(
                _UNKNOWN_PROVIDER_IDENTITY,
                _UNKNOWN_GENERATION,
            )
            external = _wait_for_external_selection(
                fixture.paths,
                _UNKNOWN_PROVIDER_IDENTITY,
            )
            assert external.account_id is None
            assert (
                tuple(
                    account.account_id
                    for account in _account_store(
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
