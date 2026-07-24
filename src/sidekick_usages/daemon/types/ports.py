"""Structural ports for resident control and isolated worker boundaries."""

from collections.abc import Iterator
from typing import Protocol

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.daemon.models.peer import PeerIdentity
from sidekick_usages.daemon.models.protocol import (
    ControlEvent,
    ControlRequest,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import (
    WorkerLaunchSpec,
    WorkerResult,
)
from sidekick_usages.daemon.types.worker import ExitNotifier


class ControlDispatcher(Protocol):
    """Dispatch already-authenticated closed control requests."""

    def dispatch(self, request: ControlRequest) -> Iterator[ControlEvent]:
        """Yield sanitized events for one accepted request."""

    def cancel(self, request_id: RequestId) -> None:
        """Cancel work whose event stream disconnected."""


class PeerSocket(Protocol):
    """Socket operations required for operating-system peer proof."""

    def fileno(self) -> int:
        """Return the live file descriptor."""

    def getsockopt(
        self,
        level: int,
        option: int,
        buffer_length: int,
        /,
    ) -> bytes:
        """Read one socket option."""


class PeerVerifier(Protocol):
    """Prove that a local connection belongs to the effective user."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Return a verified identity or fail closed."""


class WorkerHandle(Protocol):
    """One killable isolated process."""

    @property
    def process_id(self) -> int:
        """Return the native process identifier."""

    def poll(self) -> int | None:
        """Return its exit status when reaped."""

    def wait(self, timeout_seconds: float | None) -> int | None:
        """Wait up to a bound and return ``None`` on timeout."""

    def terminate_group(self) -> None:
        """Request termination of the worker process group."""

    def kill_group(self) -> None:
        """Force termination of the worker process group."""


class WorkerLauncher(Protocol):
    """Launch one exact worker specification."""

    def launch(
        self,
        spec: WorkerLaunchSpec,
        notify_exit: ExitNotifier,
    ) -> WorkerHandle:
        """Start one isolated process and arrange one exit notification."""


class OperationEventSink(Protocol):
    """Receive sanitized scheduler lifecycle events."""

    def started(self, operation: DueOperation) -> None:
        """Observe one durably running operation."""

    def completed(self, completion: SchedulerCompletion) -> None:
        """Observe one result after its queue mutation commits."""

    def failed(self, operation: DueOperation, code: str) -> None:
        """Observe a safe scheduler coordination failure."""


class WorkerExecutor(Protocol):
    """Execute one already-qualified durable operation."""

    def execute(self, operation: DueOperation) -> WorkerResult:
        """Return one sanitized result for the exact operation."""
