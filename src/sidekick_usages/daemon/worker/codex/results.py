"""Shared Codex worker result and exchange validation."""

from collections.abc import Callable

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.worker.exchange import (
    WORKER_EXCHANGE_COMPLETION_TAIL_SECONDS,
)
from sidekick_usages.daemon.worker.runtime import managed_worker_result
from sidekick_usages.providers.codex.broker.models import (
    CodexExchangeDeadlines,
)


def codex_managed_worker_result(
    operation: DueOperation,
    result: CodexManagedAuthorityResult,
    clock: Clock,
) -> WorkerResult:
    """Translate one managed Codex result into the worker protocol."""
    return managed_worker_result(
        operation,
        clock,
        succeeded=result.outcome is CodexManagedOutcome.HEALTHY,
        action_required=result.outcome.action_required,
        timed_out=result.outcome is CodexManagedOutcome.TIMED_OUT,
        failure_code=f"codex_managed_{result.outcome.value}",
    )


def codex_exchange_deadlines_current(
    deadlines: CodexExchangeDeadlines,
    monotonic: Callable[[], float],
) -> bool:
    """Return whether worker exchange deadlines remain usable."""
    return (
        deadlines.response_deadline_seconds > monotonic()
        and deadlines.completion_deadline_seconds
        >= deadlines.response_deadline_seconds
        + WORKER_EXCHANGE_COMPLETION_TAIL_SECONDS
    )
