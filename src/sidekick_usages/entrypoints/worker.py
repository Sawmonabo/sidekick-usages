"""Isolated worker process composition root."""

import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    ProviderAuthObservation,
    activation_account_ids,
)
from sidekick_usages.core.selection.types import OperationKind
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
    WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY,
)
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
    OperationAuthorityLock,
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.auth.storage import observe_native_auth

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_STATE_UNAVAILABLE = 3
_PROVIDER_AUTHORITY_TIMEOUT_SECONDS = 0.25
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
        if operation.kind in _ACCOUNT_OPERATION_KINDS:
            completed = _run_account_operation(
                operation_id,
                operation,
                paths,
                queue,
                clock,
            )
        elif operation.kind in _PROVIDER_OPERATION_KINDS:
            completed = _run_provider_operation(
                operation_id,
                operation,
                paths,
                queue,
                clock,
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
) -> bool:
    if WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY in os.environ:
        return False
    persistence = PersistenceService(
        paths,
        maintenance_quiescent=lambda: True,
    )
    store = persistence.open_store()
    with ExitStack() as resources:
        if operation.provider_id is ProviderId.CLAUDE:
            profiles = persistence.managed_claude_profiles
            selected = SelectedStateStore(paths.selected_state)
            runtime = ClaudeActivationRuntime(environment=os.environ)
            capabilities = ClaudeProfileCapabilityFactory(
                paths,
                profiles,
                environment=os.environ,
            )
            authorities = ClaudeActivationAuthorityCoordinator(
                paths,
                store,
                profiles,
                clock,
                capabilities=capabilities,
                runtime=runtime,
            )
            executor = ClaudeManagedMaintenanceWorkerExecutor(
                ClaudeManagedAuthorityCoordinator(
                    paths,
                    store,
                    profiles,
                    selected,
                    authorities,
                    capabilities,
                    clock,
                    environment=os.environ,
                ),
                clock,
            )
        else:
            coordinator = compose_codex_managed_authority(
                paths,
                store,
                persistence.managed_codex_profiles,
                clock,
                os.environ,
            )
            http = resources.enter_context(HttpClient(clock=clock))
            executor = CodexManagedMaintenanceWorkerExecutor(
                coordinator,
                CodexManagedAccountService(
                    coordinator,
                    store,
                    http,
                    ActivitySnapshotStore(paths.activity_snapshots),
                    UsageSnapshotStore(paths.usage_snapshots),
                    clock,
                ),
                clock,
            )
        return run_isolated_worker(
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


def _run_provider_operation(
    operation_id: OperationId,
    operation: DueOperation,
    paths: ApplicationPaths,
    queue: OperationQueueStore,
    clock: Clock,
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
        if (
            operation.kind is OperationKind.RECONCILE_NATIVE
            and operation.provider_id is ProviderId.CODEX
        ):
            executor = CodexNativeReconciliationWorkerExecutor(
                CodexNativeReconciliationService(
                    store,
                    CodexManagedAuthReader(
                        paths,
                        persistence.managed_codex_profiles,
                    ),
                    journals,
                    selected,
                    clock,
                ),
                RuntimeAuthObservationStore(paths.durable_operations),
                clock,
            )
        elif operation.provider_id is ProviderId.CODEX:
            executor = _codex_exchange_executor(
                operation,
                paths,
                persistence,
                store,
                selected,
                journals,
                exchange,
                clock,
            )
        elif operation.provider_id is ProviderId.CLAUDE:
            executor = _claude_selection_executor(
                operation,
                paths,
                persistence,
                store,
                selected,
                journals,
                clock,
            )
        else:
            raise ValueError("Provider worker operation is unsupported.")
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
            executor,
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
) -> CodexCallbackWorkerExecutor | CodexActivationWorkerExecutor:
    if exchange is None:
        raise ValueError("Codex exchange operation has no channel.")
    coordinator = compose_codex_managed_authority(
        paths,
        store,
        persistence.managed_codex_profiles,
        clock,
        os.environ,
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
