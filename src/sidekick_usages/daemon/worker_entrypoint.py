"""Internal isolated worker console entry point."""

import sys
from collections.abc import Sequence

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.daemon.worker_runtime import (
    UnsupportedWorkerExecutor,
    run_isolated_worker,
)
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.operation_authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.operation_queue import OperationQueueStore
from sidekick_usages.persistence.worker_results import WorkerResultStore

__all__ = ["main"]

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_STATE_UNAVAILABLE = 3


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
        completed = run_isolated_worker(
            operation_id,
            queue,
            WorkerResultStore(paths.durable_operations),
            OperationAuthorityLock(
                paths.durable_operations,
                operation.account_id,
            ),
            UnsupportedWorkerExecutor(clock),
            clock,
        )
    except OSError, TypeError, ValueError:
        return _EXIT_STATE_UNAVAILABLE
    return _EXIT_OK if completed else _EXIT_STATE_UNAVAILABLE
