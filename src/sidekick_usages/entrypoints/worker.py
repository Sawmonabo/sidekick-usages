"""Isolated worker process composition root."""

import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from functools import partial
from pathlib import Path

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.accounts.models import ClaudeAccountAuthority
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    ProviderAuthObservation,
    activation_account_ids,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationRuntime,
)
from sidekick_usages.credentials.claude.activation.reconciliation import (
    ClaudeNativeReconciliationService,
)
from sidekick_usages.credentials.claude.activation.recovery import (
    ClaudeActivationRecoveryService,
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.credentials.claude.managed.maintenance.service import (
    ClaudeManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.credentials.codex.activation import (
    CodexActivationService,
)
from sidekick_usages.credentials.codex.managed.composition import (
    compose_codex_managed_authority,
)
from sidekick_usages.credentials.codex.managed.home import (
    CodexManagedAuthReader,
)
from sidekick_usages.credentials.codex.reconciliation import (
    CodexNativeReconciliationService,
)
from sidekick_usages.daemon.models.worker import (
    WORKER_CLAUDE_EXECUTABLE_ENVIRONMENT_KEY,
    WORKER_CODEX_EXECUTABLE_ENVIRONMENT_KEY,
    WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY,
    ProviderExecutablePins,
    WorkerResult,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.account import (
    CodexManagedAccountService,
)
from sidekick_usages.daemon.worker.claude.maintenance import (
    ClaudeManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.daemon.worker.claude.selection import (
    ClaudeSelectionWorkerExecutor,
)
from sidekick_usages.daemon.worker.codex.activation import (
    CodexActivationWorkerExecutor,
)
from sidekick_usages.daemon.worker.codex.callback import (
    CodexCallbackWorkerExecutor,
)
from sidekick_usages.daemon.worker.codex.maintenance import (
    CodexManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.daemon.worker.codex.reconciliation import (
    CodexNativeReconciliationWorkerExecutor,
)
from sidekick_usages.daemon.worker.exchange import (
    WorkerExchangeChannel,
    WorkerExchangeError,
    operation_requires_worker_exchange,
)
from sidekick_usages.daemon.worker.runtime import (
    UnsupportedWorkerExecutor,
    run_isolated_worker,
    run_provider_worker,
    worker_failure,
    worker_success,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.snapshots.activity.store import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    OperationAuthorityLock,
    ProviderMutationAuthority,
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.managed.executable import (
    discover_pinned_claude_executable,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.auth.storage import observe_native_auth

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_STATE_UNAVAILABLE = 3
_PROVIDER_AUTHORITY_TIMEOUT_SECONDS = 0.25
_MANAGED_AUTH_MIGRATION_REQUIRED_CODE = "managed_auth_migration_required"
_PROVIDER_OPERATION_KINDS = frozenset(
    {
        OperationKind.CODEX_CALLBACK,
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }
)
_ACCOUNT_OPERATION_KINDS = frozenset(
    {
        OperationKind.MAINTAIN,
        OperationKind.REFRESH,
    }
)


def _worker_provider_executable_pins() -> ProviderExecutablePins:
    """Consume the supervisor-qualified provider executable paths."""
    return ProviderExecutablePins(
        claude=_worker_executable_path(
            WORKER_CLAUDE_EXECUTABLE_ENVIRONMENT_KEY
        ),
        codex=_worker_executable_path(WORKER_CODEX_EXECUTABLE_ENVIRONMENT_KEY),
    )


def _worker_executable_path(environment_key: str) -> Path | None:
    """Consume one optional absolute worker executable path."""
    raw_path = os.environ.pop(environment_key, None)
    if raw_path is None:
        return None
    return Path(raw_path)


def _codex_app_server_failure(
    operation: DueOperation,
    error: CodexAppServerError,
    clock: Clock,
) -> WorkerResult:
    """Return one durable result for a qualified Codex boundary failure."""
    return worker_failure(
        operation,
        WorkerOutcome.TRANSIENT_FAILURE,
        error.code.value,
        clock,
    )


class _CodexNativeAuthObserver:
    """Worker-local secret-free native authentication observer."""

    def __init__(self, native_home: Path, clock: Clock) -> None:
        self._native_home = native_home
        self._clock = clock

    def observe(self) -> ProviderAuthObservation:
        """Return one native observation without retaining credentials."""
        return observe_native_auth(
            credential_home=self._native_home,
            observed_at=self._clock.now(),
        )


class _AccountMaintenanceExecutor:
    """Compose one qualified account operation inside its failure boundary."""

    def __init__(
        self,
        paths: ApplicationPaths,
        clock: Clock,
        provider_executables: ProviderExecutablePins,
    ) -> None:
        self._paths = paths
        self._clock = clock
        self._provider_executables = provider_executables

    def execute(
        self,
        operation: DueOperation,
        authority: OperationAuthority,
    ) -> WorkerResult:
        """Run managed maintenance or require its one-time migration."""
        authority.require(operation.required_account_id)
        persistence = PersistenceService(
            self._paths,
            maintenance_quiescent=lambda: True,
        )
        store = persistence.open_store()
        account = store.read_saved(operation.required_account_id)
        if account is None or account.provider_id is not operation.provider_id:
            raise ValueError("Worker account authority is unavailable.")
        if not account.has_managed_authority:
            account_authority = account.authority
            if (
                isinstance(account_authority, ClaudeAccountAuthority)
                and account_authority.subscription is None
                and operation.kind is OperationKind.MAINTAIN
                and operation.priority is OperationPriority.SCHEDULED
            ):
                return worker_success(operation, self._clock)
            return worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                _MANAGED_AUTH_MIGRATION_REQUIRED_CODE,
                self._clock,
            )
        with ExitStack() as resources:
            if operation.provider_id is ProviderId.CLAUDE:
                profiles = persistence.managed_claude_profiles
                selected = SelectedStateStore(self._paths.selected_state)
                runtime = ClaudeActivationRuntime(environment=os.environ)
                capabilities = ClaudeProfileCapabilityFactory(
                    self._paths,
                    profiles,
                    environment=os.environ,
                    executable_discovery=partial(
                        discover_pinned_claude_executable,
                        self._provider_executables.claude,
                    ),
                )
                authorities = ClaudeActivationAuthorityCoordinator(
                    self._paths,
                    store,
                    profiles,
                    self._clock,
                    capabilities=capabilities,
                    runtime=runtime,
                )
                executor = ClaudeManagedMaintenanceWorkerExecutor(
                    ClaudeManagedAuthorityCoordinator(
                        self._paths,
                        store,
                        profiles,
                        selected,
                        authorities,
                        capabilities,
                        self._clock,
                        environment=os.environ,
                    ),
                    self._clock,
                )
            elif operation.provider_id is ProviderId.CODEX:
                try:
                    coordinator = compose_codex_managed_authority(
                        self._paths,
                        store,
                        persistence.managed_codex_profiles,
                        self._clock,
                        os.environ,
                        executable_path=self._provider_executables.codex,
                    )
                except CodexAppServerError as error:
                    return _codex_app_server_failure(
                        operation,
                        error,
                        self._clock,
                    )
                http = resources.enter_context(HttpClient(clock=self._clock))
                executor = CodexManagedMaintenanceWorkerExecutor(
                    coordinator,
                    CodexManagedAccountService(
                        coordinator,
                        store,
                        http,
                        ActivitySnapshotStore(self._paths.activity_snapshots),
                        UsageSnapshotStore(self._paths.usage_snapshots),
                        self._clock,
                    ),
                    self._clock,
                )
            else:
                raise ValueError("Account worker provider is unsupported.")
            return executor.execute(operation, authority)


class _ProviderOperationExecutor:
    """Compose provider selection work inside the durable result boundary."""

    def __init__(
        self,
        paths: ApplicationPaths,
        persistence: PersistenceService,
        store: AccountStore,
        selected: SelectedStateStore,
        journals: ActivationJournalStore,
        exchange: WorkerExchangeChannel | None,
        clock: Clock,
        provider_executables: ProviderExecutablePins,
    ) -> None:
        self._paths = paths
        self._persistence = persistence
        self._store = store
        self._selected = selected
        self._journals = journals
        self._exchange = exchange
        self._clock = clock
        self._provider_executables = provider_executables

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Compose and execute one provider operation under its held lock."""
        if (
            operation.kind is OperationKind.RECONCILE_NATIVE
            and operation.provider_id is ProviderId.CODEX
        ):
            executor = CodexNativeReconciliationWorkerExecutor(
                CodexNativeReconciliationService(
                    self._store,
                    CodexManagedAuthReader(
                        self._paths,
                        self._persistence.managed_codex_profiles,
                    ),
                    self._journals,
                    self._selected,
                    self._clock,
                ),
                RuntimeAuthObservationStore(self._paths.durable_operations),
                self._clock,
            )
            return executor.execute(operation, authority)
        if operation.provider_id is ProviderId.CODEX:
            try:
                executor = _codex_exchange_executor(
                    operation,
                    self._paths,
                    self._persistence,
                    self._store,
                    self._selected,
                    self._journals,
                    self._exchange,
                    self._clock,
                    self._provider_executables.codex,
                )
            except CodexAppServerError as error:
                return _codex_app_server_failure(
                    operation,
                    error,
                    self._clock,
                )
            return executor.execute(operation, authority)
        if operation.provider_id is ProviderId.CLAUDE:
            executor = _claude_selection_executor(
                operation,
                self._paths,
                self._persistence,
                self._store,
                self._selected,
                self._journals,
                self._clock,
                self._provider_executables.claude,
            )
            return executor.execute(operation, authority)
        raise ValueError("Provider worker operation is unsupported.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run exactly one operation identified by its sole argument."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return _EXIT_INVALID_INVOCATION
    try:
        operation_id = OperationId(arguments[0])
        paths = discover_application_paths()
        queue = OperationQueueStore(paths.durable_operations)
        operation = queue.find(operation_id)
        if operation is None:
            return _EXIT_STATE_UNAVAILABLE
        clock = SystemClock()
        provider_executables = _worker_provider_executable_pins()
        if operation.kind in _ACCOUNT_OPERATION_KINDS:
            completed = _run_account_operation(
                operation_id,
                operation,
                paths,
                queue,
                clock,
                provider_executables,
            )
        elif operation.kind in _PROVIDER_OPERATION_KINDS:
            completed = _run_provider_operation(
                operation_id,
                operation,
                paths,
                queue,
                clock,
                provider_executables,
            )
        else:
            if WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY in os.environ:
                return _EXIT_STATE_UNAVAILABLE
            executor = UnsupportedWorkerExecutor(clock)
            completed = run_isolated_worker(
                operation_id,
                queue,
                WorkerResultStore(paths.durable_operations),
                OperationAuthorityLock(
                    paths.durable_operations,
                    operation.required_account_id,
                ),
                executor,
                clock,
            )
    except (
        CodexAppServerError,
        OSError,
        PersistenceError,
        TypeError,
        ValueError,
        WorkerExchangeError,
    ):
        return _EXIT_STATE_UNAVAILABLE
    return _EXIT_OK if completed else _EXIT_STATE_UNAVAILABLE


def _run_account_operation(
    operation_id: OperationId,
    operation: DueOperation,
    paths: ApplicationPaths,
    queue: OperationQueueStore,
    clock: Clock,
    provider_executables: ProviderExecutablePins,
) -> bool:
    if WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY in os.environ:
        return False
    return run_isolated_worker(
        operation_id,
        queue,
        WorkerResultStore(paths.durable_operations),
        OperationAuthorityLock(
            paths.durable_operations,
            operation.required_account_id,
        ),
        _AccountMaintenanceExecutor(paths, clock, provider_executables),
        clock,
    )


def _run_provider_operation(
    operation_id: OperationId,
    operation: DueOperation,
    paths: ApplicationPaths,
    queue: OperationQueueStore,
    clock: Clock,
    provider_executables: ProviderExecutablePins,
) -> bool:
    exchange = (
        WorkerExchangeChannel.from_environment()
        if operation_requires_worker_exchange(operation)
        else None
    )
    if (
        exchange is None
        and WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY in os.environ
    ):
        return False
    try:
        persistence = PersistenceService(
            paths,
            maintenance_quiescent=lambda: True,
        )
        store = persistence.open_store()
        selected = SelectedStateStore(paths.selected_state)
        journals = ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        )
        return run_provider_worker(
            operation_id,
            queue,
            WorkerResultStore(paths.durable_operations),
            ProviderMutationLock(
                paths.durable_operations,
                operation.provider_id,
                _provider_account_ids(
                    operation,
                    store,
                    selected,
                    journals,
                ),
                timeout_seconds=_PROVIDER_AUTHORITY_TIMEOUT_SECONDS,
            ),
            _ProviderOperationExecutor(
                paths,
                persistence,
                store,
                selected,
                journals,
                exchange,
                clock,
                provider_executables,
            ),
            clock,
        )
    finally:
        if exchange is not None:
            exchange.close()


def _codex_exchange_executor(
    operation: DueOperation,
    paths: ApplicationPaths,
    persistence: PersistenceService,
    store: AccountStore,
    selected: SelectedStateStore,
    journals: ActivationJournalStore,
    exchange: WorkerExchangeChannel | None,
    clock: Clock,
    codex_executable: Path | None,
) -> CodexCallbackWorkerExecutor | CodexActivationWorkerExecutor:
    if exchange is None:
        raise ValueError("Codex exchange operation has no channel.")
    coordinator = compose_codex_managed_authority(
        paths,
        store,
        persistence.managed_codex_profiles,
        clock,
        os.environ,
        executable_path=codex_executable,
    )
    if operation.kind is OperationKind.CODEX_CALLBACK:
        return CodexCallbackWorkerExecutor(
            coordinator,
            selected,
            exchange,
            clock,
        )
    return CodexActivationWorkerExecutor(
        CodexActivationService(
            coordinator,
            journals,
            selected,
            _CodexNativeAuthObserver(
                default_codex_home(),
                clock,
            ),
            clock,
        ),
        exchange,
        clock,
    )


def _claude_selection_executor(
    operation: DueOperation,
    paths: ApplicationPaths,
    persistence: PersistenceService,
    store: AccountStore,
    selected: SelectedStateStore,
    journals: ActivationJournalStore,
    clock: Clock,
    claude_executable: Path | None,
) -> ClaudeSelectionWorkerExecutor:
    if operation.kind not in {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }:
        raise ValueError("Claude selection operation is unsupported.")
    runtime = ClaudeActivationRuntime(environment=os.environ)
    profiles = persistence.managed_claude_profiles
    capabilities = ClaudeProfileCapabilityFactory(
        paths,
        profiles,
        environment=os.environ,
        executable_discovery=partial(
            discover_pinned_claude_executable,
            claude_executable,
        ),
    )
    authorities = ClaudeActivationAuthorityCoordinator(
        paths,
        store,
        profiles,
        clock,
        capabilities=capabilities,
        runtime=runtime,
    )
    recovery = ClaudeActivationRecoveryService(
        authorities,
        journals,
        selected,
        clock,
    )
    return ClaudeSelectionWorkerExecutor(
        ClaudeActivationService(
            authorities,
            journals,
            selected,
            clock,
        ),
        recovery,
        ClaudeNativeReconciliationService(
            authorities,
            recovery,
            journals,
            selected,
            clock,
        ),
        clock,
    )


def _provider_account_ids(
    operation: DueOperation,
    store: AccountStore,
    selected: SelectedStateStore,
    journals: ActivationJournalStore,
) -> tuple[SidekickAccountId, ...]:
    """Resolve the exact provider-first account lock set before execution."""
    if operation.kind is OperationKind.CODEX_CALLBACK:
        return (operation.required_account_id,)
    if operation.kind is OperationKind.RECONCILE_NATIVE:
        return tuple(
            sorted(
                account.account_id
                for account in store.saved_accounts()
                if account.provider_id is operation.provider_id
            )
        )
    if (
        operation.kind is OperationKind.RECONCILE
        and operation.provider_id is ProviderId.CLAUDE
    ):
        return tuple(
            sorted(
                account.account_id
                for account in store.saved_accounts(ProviderId.CLAUDE)
            )
        )
    if operation.kind not in {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
    }:
        raise ValueError("Provider operation cannot acquire activation locks.")
    account_id = operation.required_account_id
    if operation.kind is OperationKind.RECONCILE:
        active = journals.load(operation.provider_id).active
        if active is None or active.target_account_id != account_id:
            raise ValueError("Provider reconciliation journal is unavailable.")
        baseline = active.selected_baseline
    else:
        baseline = selected.load(operation.provider_id)
    return tuple(sorted(activation_account_ids(baseline, account_id)))
