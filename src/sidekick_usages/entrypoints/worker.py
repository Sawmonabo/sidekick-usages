"""Isolated worker process composition root."""

import os
import sys
from collections.abc import Sequence

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.types import OperationKind
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.daemon.models.worker import (
    CALLBACK_DESCRIPTOR_ENVIRONMENT_KEY,
)
from sidekick_usages.daemon.worker.codex import CodexCallbackWorkerExecutor
from sidekick_usages.daemon.worker.exchange import (
    CallbackExchangeError,
    WorkerCallbackChannel,
)
from sidekick_usages.daemon.worker.runtime import (
    UnsupportedWorkerExecutor,
    run_isolated_worker,
    run_provider_worker,
)
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.service import PersistenceService
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

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_STATE_UNAVAILABLE = 3
_CALLBACK_AUTHORITY_TIMEOUT_SECONDS = 0.25


def main(argv: Sequence[str] | None = None) -> int:
    """Run exactly one operation identified by its sole argument."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return _EXIT_INVALID_INVOCATION
    channel: WorkerCallbackChannel | None = None
    try:
        operation_id = OperationId(arguments[0])
        paths = discover_application_paths()
        queue = OperationQueueStore(paths.durable_operations)
        operation = queue.find(operation_id)
        if operation is None:
            return _EXIT_STATE_UNAVAILABLE
        clock = SystemClock()
        if operation.kind is OperationKind.CODEX_CALLBACK:
            channel = WorkerCallbackChannel.from_environment()
            persistence = PersistenceService(
                paths,
                maintenance_quiescent=lambda: True,
            )
            executor = CodexCallbackWorkerExecutor(
                CodexManagedAuthorityCoordinator(
                    paths,
                    persistence.open_store(),
                    persistence.managed_codex_profiles,
                    probe_codex_capabilities(
                        discover_codex_executable(
                            os.environ,
                            process_group=(CodexProcessGroupPolicy.INHERITED),
                        ),
                        os.environ,
                        process_group=CodexProcessGroupPolicy.INHERITED,
                    ),
                    clock,
                    environment=os.environ,
                ),
                SelectedStateStore(paths.selected_state),
                channel,
                clock,
            )
            completed = run_provider_worker(
                operation_id,
                queue,
                WorkerResultStore(paths.durable_operations),
                ProviderMutationLock(
                    paths.durable_operations,
                    operation.provider_id,
                    (operation.account_id,),
                    timeout_seconds=_CALLBACK_AUTHORITY_TIMEOUT_SECONDS,
                ),
                executor,
                clock,
            )
        else:
            if CALLBACK_DESCRIPTOR_ENVIRONMENT_KEY in os.environ:
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
        CallbackExchangeError,
        CodexAppServerError,
        OSError,
        PersistenceError,
        TypeError,
        ValueError,
    ):
        return _EXIT_STATE_UNAVAILABLE
    finally:
        if channel is not None:
            channel.close()
    return _EXIT_OK if completed else _EXIT_STATE_UNAVAILABLE
