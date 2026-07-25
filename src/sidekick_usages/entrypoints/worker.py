"""Isolated worker process composition root."""

import os
import sys
from collections.abc import Sequence
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
from sidekick_usages.credentials.codex.activation import (
    CodexActivationService,
)
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.daemon.models.worker import (
    WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY,
)
from sidekick_usages.daemon.worker.codex import (
    CodexActivationWorkerExecutor,
    CodexCallbackWorkerExecutor,
)
from sidekick_usages.daemon.worker.exchange import (
    WorkerExchangeChannel,
    WorkerExchangeError,
)
from sidekick_usages.daemon.worker.runtime import (
    UnsupportedWorkerExecutor,
    run_isolated_worker,
    run_provider_worker,
)
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
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
from sidekick_usages.providers.codex.app_server.types import (
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.auth import observe_native_auth
from sidekick_usages.providers.codex.native import default_codex_home

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_STATE_UNAVAILABLE = 3
_PROVIDER_AUTHORITY_TIMEOUT_SECONDS = 0.25
_EXCHANGE_OPERATION_KINDS = frozenset(
    {
        OperationKind.CODEX_CALLBACK,
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
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
    exchange: WorkerExchangeChannel | None = None
    try:
        operation_id = OperationId(arguments[0])
        paths = discover_application_paths()
        queue = OperationQueueStore(paths.durable_operations)
        operation = queue.find(operation_id)
        if operation is None:
            return _EXIT_STATE_UNAVAILABLE
        clock = SystemClock()
        if operation.kind in _EXCHANGE_OPERATION_KINDS:
            exchange = WorkerExchangeChannel.from_environment()
            persistence = PersistenceService(
                paths,
                maintenance_quiescent=lambda: True,
            )
            selected = SelectedStateStore(paths.selected_state)
            journals = ActivationJournalStore(
                paths.activation_journals,
                paths.durable_operations,
            )
            coordinator = CodexManagedAuthorityCoordinator(
                paths,
                persistence.open_store(),
                persistence.managed_codex_profiles,
                probe_codex_capabilities(
                    discover_codex_executable(
                        os.environ,
                        process_group=CodexProcessGroupPolicy.INHERITED,
                    ),
                    os.environ,
                    process_group=CodexProcessGroupPolicy.INHERITED,
                ),
                clock,
                environment=os.environ,
            )
            executor = (
                CodexCallbackWorkerExecutor(
                    coordinator,
                    selected,
                    exchange,
                    clock,
                )
                if operation.kind is OperationKind.CODEX_CALLBACK
                else CodexActivationWorkerExecutor(
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
            )
            completed = run_provider_worker(
                operation_id,
                queue,
                WorkerResultStore(paths.durable_operations),
                ProviderMutationLock(
                    paths.durable_operations,
                    operation.provider_id,
                    _provider_account_ids(
                        operation,
                        selected,
                        journals,
                    ),
                    timeout_seconds=_PROVIDER_AUTHORITY_TIMEOUT_SECONDS,
                ),
                executor,
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
                    operation.account_id,
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
    finally:
        if exchange is not None:
            exchange.close()
    return _EXIT_OK if completed else _EXIT_STATE_UNAVAILABLE


def _provider_account_ids(
    operation: DueOperation,
    selected: SelectedStateStore,
    journals: ActivationJournalStore,
) -> tuple[SidekickAccountId, ...]:
    """Resolve the exact provider-first account lock set before execution."""
    if operation.kind is OperationKind.CODEX_CALLBACK:
        return (operation.account_id,)
    if operation.provider_id is not ProviderId.CODEX:
        raise ValueError("Managed exchange operation is not Codex.")
    if operation.kind is OperationKind.RECONCILE:
        active = journals.load(ProviderId.CODEX).active
        if active is None or active.target_account_id != operation.account_id:
            raise ValueError("Codex reconciliation journal is unavailable.")
        baseline = active.selected_baseline
    else:
        baseline = selected.load(ProviderId.CODEX)
    return tuple(
        sorted(activation_account_ids(baseline, operation.account_id))
    )
