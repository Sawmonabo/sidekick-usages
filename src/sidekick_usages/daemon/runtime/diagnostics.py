"""Sanitized bounded supervisor diagnostics."""

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.daemon.models.diagnostics import (
    MAX_DIAGNOSTIC_DURATION_MILLISECONDS,
    DiagnosticEvent,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.types.ports import OperationEventSink
from sidekick_usages.daemon.types.service import PackageVersion

_LOG_BASENAME = "supervisor.jsonl"
_MAX_LOG_BYTES = 256 * 1024
_RETAINED_LOGS = 3
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class SanitizedDiagnosticLog:
    """Append owner-only JSON lines with a small fixed rotation bound."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("Service log root must be absolute.")
        self._root = root
        self._path = root / _LOG_BASENAME

    def append(self, event: DiagnosticEvent) -> None:
        """Append one bounded no-secret event."""
        payload = _encode_event(event)
        self._prepare_root()
        self._rotate_if_needed(len(payload))
        flags = os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, _FILE_MODE)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
            ):
                raise OSError("Unsafe supervisor diagnostic file.")
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("Incomplete supervisor diagnostic write.")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def clear(self) -> None:
        """Remove only known supervisor diagnostic files."""
        if not self._root.exists():
            return
        self._require_safe_root()
        paths = (
            self._path,
            *(self._rotated(index) for index in range(1, _RETAINED_LOGS + 1)),
            self._root / "supervisor.out.log",
            self._root / "supervisor.err.log",
        )
        for path in paths:
            self._remove_known_file(path)
        try:
            self._root.rmdir()
        except OSError:
            return

    def _prepare_root(self) -> None:
        self._root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        self._require_safe_root()
        if stat.S_IMODE(self._root.stat().st_mode) != _DIRECTORY_MODE:
            os.chmod(self._root, _DIRECTORY_MODE)
            self._require_safe_root()

    def _require_safe_root(self) -> None:
        metadata = self._root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("Unsafe supervisor diagnostic directory.")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("Unsafe supervisor diagnostic file.")
        if metadata.st_size + incoming_bytes <= _MAX_LOG_BYTES:
            return
        oldest = self._rotated(_RETAINED_LOGS)
        if oldest.exists():
            oldest_metadata = oldest.lstat()
            if (
                not stat.S_ISREG(oldest_metadata.st_mode)
                or stat.S_ISLNK(oldest_metadata.st_mode)
                or oldest_metadata.st_uid != os.geteuid()
            ):
                raise OSError("Unsafe rotated supervisor diagnostic file.")
            oldest.unlink()
        for index in range(_RETAINED_LOGS - 1, 0, -1):
            source = self._rotated(index)
            if source.exists():
                os.replace(source, self._rotated(index + 1))
        os.replace(self._path, self._rotated(1))

    def _rotated(self, index: int) -> Path:
        return self._root / f"{_LOG_BASENAME}.{index}"

    @staticmethod
    def _remove_known_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("Unsafe supervisor diagnostic file.")
        path.unlink()


class DiagnosticOperationSink(OperationEventSink):
    """Record only bounded account IDs and safe operation outcomes."""

    def __init__(
        self,
        log: SanitizedDiagnosticLog,
        clock: Clock,
        monotonic: Callable[[], float],
        *,
        package_version: str = __version__,
    ) -> None:
        self._log = log
        self._clock = clock
        self._monotonic = monotonic
        self._package_version = PackageVersion(package_version)
        self._active: dict[OperationId, tuple[DueOperation, float]] = {}

    def started(self, operation: DueOperation) -> None:
        """Record one started worker without command or environment data."""
        started = self._monotonic()
        self._active[operation.operation_id] = (operation, started)
        self._append(
            operation,
            phase="worker_started",
            result="running",
            duration=0,
        )

    def completed(self, completion: SchedulerCompletion) -> None:
        """Record one committed terminal queue result."""
        active = self._active.pop(completion.operation_id, None)
        if active is None:
            self._append_unknown(completion)
            return
        operation, started = active
        duration = max(0.0, self._monotonic() - started)
        self._append(
            operation,
            phase="worker_completed",
            result=(completion.failure_code or completion.outcome.value),
            duration=_duration_milliseconds(duration),
        )

    def failed(self, operation: DueOperation, code: str) -> None:
        """Record one sanitized coordination failure."""
        self._active.pop(operation.operation_id, None)
        self._append(
            operation,
            phase="worker_failed",
            result=code,
            duration=0,
        )

    def _append(
        self,
        operation: DueOperation,
        *,
        phase: str,
        result: str,
        duration: int,
    ) -> None:
        self._log.append(
            DiagnosticEvent(
                observed_at=self._clock.now(),
                operation_id=operation.operation_id,
                account_id=operation.account_id,
                provider_id=operation.provider_id,
                phase=phase,
                result=result,
                duration_milliseconds=duration,
                package_version=self._package_version,
            )
        )

    def _append_unknown(self, completion: SchedulerCompletion) -> None:
        self._log.append(
            DiagnosticEvent(
                observed_at=self._clock.now(),
                operation_id=completion.operation_id,
                phase="worker_recovered",
                result=completion.failure_code or completion.outcome.value,
                duration_milliseconds=0,
                package_version=self._package_version,
            )
        )


class CompositeOperationSink(OperationEventSink):
    """Fan one sanitized event into a small fixed set of sinks."""

    def __init__(self, *sinks: OperationEventSink) -> None:
        if not sinks:
            raise ValueError("At least one operation event sink is required.")
        self._sinks = sinks

    def started(self, operation: DueOperation) -> None:
        for sink in self._sinks:
            sink.started(operation)

    def completed(self, completion: SchedulerCompletion) -> None:
        for sink in self._sinks:
            sink.completed(completion)

    def failed(self, operation: DueOperation, code: str) -> None:
        for sink in self._sinks:
            sink.failed(operation, code)


def _duration_milliseconds(seconds: float) -> int:
    return min(
        int(seconds * 1000),
        MAX_DIAGNOSTIC_DURATION_MILLISECONDS,
    )


def _encode_event(event: DiagnosticEvent) -> bytes:
    root = {
        "account_id": (
            None if event.account_id is None else str(event.account_id)
        ),
        "duration_milliseconds": event.duration_milliseconds,
        "observed_at": event.observed_at.isoformat().replace("+00:00", "Z"),
        "operation_id": (
            None if event.operation_id is None else str(event.operation_id)
        ),
        "package_version": str(event.package_version),
        "phase": event.phase,
        "provider_id": (
            None if event.provider_id is None else event.provider_id.value
        ),
        "result": event.result,
    }
    return (
        json.dumps(
            root,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
