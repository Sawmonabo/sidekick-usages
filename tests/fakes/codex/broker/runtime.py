"""Reusable managed Codex runtime fixtures and observations."""

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
    FinalizedSelection,
    ProviderAuthObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.home import (
    CodexManagedAuthReader,
)
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
)
from sidekick_usages.paths import (
    ApplicationPaths,
    discover_application_paths,
    managed_codex_home,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
)
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import write_fake_managed_codex
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import NEXT_AUTH_FILE, managed_auth
from tests.fakes.codex.broker.models import FakeCodexBrokerFixture
from tests.fakes.codex.broker.supervisor import FakeCodexSupervisor
from tests.fakes.codex.managed import (
    managed_saved_account,
    managed_subscription,
    seed_managed_accounts,
)
from tests.support.persistence import seed_finalized_selections
from tests.support.time import REFERENCE_TIME, FixedClock

ACCOUNT_A_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
ACCOUNT_A_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACCOUNT_A_PROVIDER_IDENTITY = "workspace-account-unselected"
MANAGED_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
MANAGED_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
PROVIDER_IDENTITY = "workspace-account-alpha"
GENERATION = "2026-07-24T10:00:00.000000000Z"
NEXT_GENERATION = "2026-07-24T10:01:00.000000000Z"
RECOVERY_GENERATION = "2026-07-24T10:02:00.000000000Z"
UNSELECTED_NEXT_GENERATION = "2026-07-24T10:03:00.000000000Z"
UNKNOWN_GENERATION = "2026-07-24T10:04:00.000000000Z"
UNKNOWN_PROVIDER_IDENTITY = "workspace-account-external"
NATIVE_AUTH_SENTINEL = managed_auth(
    ACCOUNT_A_PROVIDER_IDENTITY,
    GENERATION,
)
_WAIT_TIMEOUT_SECONDS = 10.0
_WAIT_INTERVAL_SECONDS = 0.01


def selected_account(
    account_id: SidekickAccountId,
    provider_identity: str,
    generation: str,
) -> FinalizedSelection:
    """Build one finalized Codex selection fixture."""
    del provider_identity
    return FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=account_id,
        epoch=SelectionEpoch(0),
        generation=AuthorityGeneration(generation),
        finalized_at=REFERENCE_TIME,
    )


def activation_source_fixture(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeCodexBrokerFixture:
    """Build a runtime finalized at the matching native account A."""
    return _broker_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
        selected_account(
            ACCOUNT_A_ID,
            ACCOUNT_A_PROVIDER_IDENTITY,
            GENERATION,
        ),
    )


def broker_finalized_fixture(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeCodexBrokerFixture:
    """Build a runtime finalized at broker-managed account B."""
    return _broker_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
        selected_account(
            MANAGED_ACCOUNT_ID,
            PROVIDER_IDENTITY,
            GENERATION,
        ),
    )


def _broker_fixture(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalized: FinalizedSelection,
) -> FakeCodexBrokerFixture:
    """Build two managed accounts and an isolated resident-broker runtime."""
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    schema_root = provider_root / "schema"
    root = short_socket_root / "state"
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
    paths = discover_application_paths()
    native_home = Path(environment["HOME"]) / ".codex"
    native_home.mkdir()
    native_auth = native_home / "auth.json"
    native_auth.write_bytes(NATIVE_AUTH_SENTINEL)
    os.chmod(native_auth, 0o600)
    (native_home / "config.toml").write_text(
        "# Codex defaults to file-backed CLI authentication.\n",
        encoding="utf-8",
    )
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(provider_root, schema_root, native_home)
    account_a = managed_saved_account(
        ACCOUNT_A_ID,
        ACCOUNT_A_AUTHORITY_ID,
        "codex-unselected",
        ACCOUNT_A_PROVIDER_IDENTITY,
        GENERATION,
    )
    account_b = managed_saved_account(
        MANAGED_ACCOUNT_ID,
        MANAGED_AUTHORITY_ID,
        "codex-selected",
        PROVIDER_IDENTITY,
        GENERATION,
    )
    seeded_paths, _store, private = seed_managed_accounts(
        paths.accounts.parent,
        (account_a, account_b),
        {
            ACCOUNT_A_ID: managed_auth(
                ACCOUNT_A_PROVIDER_IDENTITY,
                UNSELECTED_NEXT_GENERATION,
            ),
            MANAGED_ACCOUNT_ID: managed_auth(
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            ),
        },
    )
    if seeded_paths.accounts != paths.accounts:
        raise AssertionError("Discovered and synthetic paths disagree.")
    seed_finalized_selections(
        paths,
        finalized,
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


def account_store(paths: ApplicationPaths) -> AccountStore:
    """Open the synthetic runtime account store."""
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return AccountStore(paths.accounts, private).load()


def saved_generation(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> str:
    """Return one saved managed-authority generation."""
    account = account_store(paths).read_saved(account_id)
    if account is None:
        raise AssertionError("Managed Codex account disappeared.")
    return str(managed_subscription(account).generation)


def wait_for_projected_generation(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
    provider_identity: str,
    generation: str,
) -> None:
    """Wait for exact protected and projected Codex authority proof."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while True:
        try:
            if (
                projection_matches_account(
                    paths,
                    account_id,
                    provider_identity,
                )
                and saved_generation(paths, account_id) == generation
            ):
                return
        except PersistenceError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Codex projected authority did not advance.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def projection_matches_account(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
    provider_identity: str,
) -> bool:
    """Return whether projection proof matches one exact private account."""
    observation = RuntimeAuthObservationStore(
        paths.durable_operations
    ).observe_projection(ProviderId.CODEX)
    if (
        observation is None
        or observation.provider_identity != ProviderIdentity(provider_identity)
    ):
        return False
    private = PrivateCredentialTree(
        paths.private_codex_profiles,
        account_path=paths.accounts,
    )
    matched = CodexManagedAuthReader(paths, private).matches_observation(
        account_id,
        observation,
    )
    return matched is True


def wait_for_external_selection(
    paths: ApplicationPaths,
    provider_identity: str,
) -> ProviderAuthObservation:
    """Wait for an external Codex login to become observed."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    observations = RuntimeAuthObservationStore(paths.durable_operations)
    while True:
        state = observations.observe_native(ProviderId.CODEX)
        if state is not None and state.provider_identity == ProviderIdentity(
            provider_identity
        ):
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                "External Codex observation was not recorded."
            )
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def wait_for_operation_state(
    queue: OperationQueueStore,
    operation_id: OperationId,
    state: OperationState,
) -> DueOperation:
    """Wait for one durable operation state."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while True:
        operation = queue.find(operation_id)
        if operation is not None and operation.state is state:
            return operation
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Durable operation did not reach its state.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def wait_for_file(path: Path) -> None:
    """Wait for a worker marker file."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while not path.is_file():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Expected worker marker was not created.")
        time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))


def real_worker_executable() -> Path:
    """Resolve the editable isolated-worker entry point."""
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


def stage_provider_ahead(fixture: FakeCodexBrokerFixture) -> None:
    """Stage provider-owned state beyond its durable saved generation."""
    paths = fixture.paths
    next_auth = managed_codex_home(paths, MANAGED_ACCOUNT_ID) / NEXT_AUTH_FILE
    next_auth.write_bytes(managed_auth(PROVIDER_IDENTITY, RECOVERY_GENERATION))
    os.chmod(next_auth, 0o600)
    coordinator = CodexManagedAuthorityCoordinator(
        paths,
        account_store(paths),
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
        MANAGED_ACCOUNT_ID,
    )
    with lock.hold() as authority:
        staged = coordinator.stage_refresh_with_authority(
            MANAGED_ACCOUNT_ID,
            authority,
            CodexProjectionExpectation(
                MANAGED_ACCOUNT_ID,
                ProviderIdentity(PROVIDER_IDENTITY),
                AuthorityGeneration(NEXT_GENERATION),
            ),
        )
    if isinstance(staged, CodexManagedAuthorityResult):
        raise AssertionError("Synthetic provider-ahead refresh failed.")


def interrupt_activation_at_install(
    supervisor: FakeCodexSupervisor,
    daemon: FakeCodexDaemon,
    paths: ApplicationPaths,
    operation_id: OperationId,
    account_id: SidekickAccountId,
) -> None:
    """Interrupt activation after the provider installs the projection."""
    daemon.pause_next_install()
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
    supervisor.notify()
    try:
        daemon.wait_for_paused_install()
    except AssertionError as error:
        operation = OperationQueueStore(paths.durable_operations).find(
            operation_id
        )
        failure_code = None if operation is None else operation.failure_code
        raise AssertionError(
            f"Fake Codex activation did not reach install: {failure_code}."
        ) from error
    supervisor.request_stop()
    try:
        supervisor.close()
    finally:
        daemon.resume_install()
    operation = OperationQueueStore(paths.durable_operations).find(
        operation_id
    )
    if operation is None or operation.state is not OperationState.RETRY_WAIT:
        raise AssertionError(
            "Interrupted durable operation was not rescheduled."
        )
