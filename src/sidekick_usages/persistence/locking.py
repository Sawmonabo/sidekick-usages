"""Bounded cooperative lock for account-persistence transactions."""

import time
from collections.abc import Callable
from enum import StrEnum
from types import TracebackType
from typing import IO

import portalocker
from portalocker import LockFlags

from sidekick_usages.persistence.errors import (
    PersistenceError,
    PersistenceFilesystemError,
)
from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
    _begin_transaction,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.types.error import PersistenceCode

LOCK_TIMEOUT_SECONDS = 5.0
LOCK_CHECK_INTERVAL_SECONDS = 0.1
_EXCLUSIVE_LOCK_FLAGS = LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
_SHARED_LOCK_FLAGS = LockFlags.SHARED | LockFlags.NON_BLOCKING


class LockFailurePhase(StrEnum):
    """Bounded stage at which the native lock primitive malfunctioned."""

    ACQUIRE = "acquire"
    RELEASE = "release"


class StoreLockedError(PersistenceError):
    """Another participating process holds the persistence lock."""

    def __init__(self) -> None:
        self.code = PersistenceCode.STORE_LOCKED
        super().__init__(
            "Account persistence is locked by another process; retry shortly."
        )


class LockUnavailableError(PersistenceError):
    """The platform hard-lock primitive malfunctioned."""

    def __init__(
        self,
        phase: LockFailurePhase = LockFailurePhase.ACQUIRE,
        *,
        handle_closed: bool,
    ) -> None:
        self.code = PersistenceCode.UNSUPPORTED_FILESYSTEM
        self.phase = phase
        self.handle_closed = handle_closed
        self.lock_may_be_held = not handle_closed
        message = (
            "Account persistence cannot acquire a supported hard lock."
            if phase is LockFailurePhase.ACQUIRE
            else "Account persistence lock release malfunctioned."
        )
        super().__init__(message)


class TransactionReleaseError(PersistenceError):
    """An owned operation failed and lock release also malfunctioned."""

    def __init__(
        self,
        operation_code: PersistenceCode,
        *,
        artifact_basename: str | None,
        handle_closed: bool,
    ) -> None:
        self.code = operation_code
        self.operation_code = operation_code
        self.artifact_basename = artifact_basename
        self.handle_closed = handle_closed
        self.lock_may_be_held = not handle_closed
        self.release_failed = True
        super().__init__(
            "Account persistence failed and lock release malfunctioned."
        )


def _operation_context(
    error: BaseException | None,
) -> tuple[PersistenceCode, str | None] | None:
    if isinstance(error, PersistenceFilesystemError):
        return error.code, error.artifact_basename
    if isinstance(error, PersistenceError):
        return error.code, None
    return None


def _close_sidecar(sidecar: IO[bytes]) -> bool:
    try:
        sidecar.close()
    except OSError:
        return False
    return True


def _release_sidecar(sidecar: IO[bytes]) -> tuple[bool, bool]:
    unlock_succeeded = True
    try:
        portalocker.unlock(sidecar)
    except portalocker.LockException, OSError:
        unlock_succeeded = False
    except BaseException as error:
        if not _close_sidecar(sidecar):
            error.add_note("Persistence lock handle cleanup also failed.")
        raise
    return unlock_succeeded, _close_sidecar(sidecar)


def _release_after_invalidation(
    sidecar: IO[bytes],
    error: BaseException,
) -> None:
    try:
        unlock_succeeded, handle_closed = _release_sidecar(sidecar)
    except BaseException:
        error.add_note("Persistence lock release also malfunctioned.")
        raise error from None
    if not unlock_succeeded or not handle_closed:
        error.add_note("Persistence lock release also malfunctioned.")
    raise error from None


def _release_after_operation(
    sidecar: IO[bytes],
    operation_error: BaseException | None,
) -> bool:
    try:
        unlock_succeeded, handle_closed = _release_sidecar(sidecar)
    except BaseException as error:
        if operation_error is not None:
            operation_error.add_note(
                "Persistence lock release also malfunctioned."
            )
            return False
        raise error from None
    if unlock_succeeded and handle_closed:
        return False
    if (context := _operation_context(operation_error)) is not None:
        code, basename = context
        raise TransactionReleaseError(
            code,
            artifact_basename=basename,
            handle_closed=handle_closed,
        ) from None
    if operation_error is not None:
        operation_error.add_note(
            "Persistence lock release also malfunctioned."
        )
        return False
    raise LockUnavailableError(
        LockFailurePhase.RELEASE,
        handle_closed=handle_closed,
    ) from None


def _wait_for_lock(
    sidecar: IO[bytes],
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    flags: LockFlags,
) -> None:
    while True:
        try:
            portalocker.lock(sidecar, flags)
        except portalocker.AlreadyLocked:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise StoreLockedError from None
            sleep(min(LOCK_CHECK_INTERVAL_SECONDS, remaining))
        else:
            return


class _HeldPersistenceLock:
    """Acquire on entry and own one lock-scoped mutation capability."""

    def __init__(self, owner: PersistenceLock) -> None:
        self._owner = owner
        self._sidecar: IO[bytes] | None = None
        self._transaction: PersistenceTransaction | None = None
        self._released = False

    def __enter__(self) -> PersistenceTransaction:
        """Acquire the lock and return its mutation capability."""
        if self._released or self._sidecar is not None:
            raise RuntimeError("Persistence lock context cannot be reused.")
        sidecar = self._owner._acquire()
        self._sidecar = sidecar
        try:
            transaction = _begin_transaction(self._owner._filesystem)
        except BaseException as error:
            self._released = True
            try:
                unlock_succeeded, handle_closed = _release_sidecar(sidecar)
            except BaseException:
                error.add_note("Persistence lock release also malfunctioned.")
                raise error from None
            if not unlock_succeeded or not handle_closed:
                if (context := _operation_context(error)) is not None:
                    code, basename = context
                    raise TransactionReleaseError(
                        code,
                        artifact_basename=basename,
                        handle_closed=handle_closed,
                    ) from None
                error.add_note("Persistence lock release also malfunctioned.")
            raise
        self._transaction = transaction
        return transaction

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Invalidate the capability, release, and close exactly once."""
        del exc_type, traceback
        if self._released:
            return False
        self._released = True
        invalidation_error: BaseException | None = None
        if self._transaction is not None:
            try:
                self._transaction._invalidate()
            except BaseException as error:
                invalidation_error = error
        sidecar = self._sidecar
        if sidecar is None:
            raise LockUnavailableError(handle_closed=True)
        if invalidation_error is not None:
            _release_after_invalidation(sidecar, invalidation_error)
        return _release_after_operation(sidecar, exc_value)


class PersistenceLock:
    """Provide a side-effect-free context for one bounded hard lock."""

    def __init__(
        self,
        filesystem: PrivateFilesystem,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
        shared: bool = False,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("Lock timeout must not be negative.")
        self._filesystem = filesystem
        self._monotonic = monotonic
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        self._flags = _SHARED_LOCK_FLAGS if shared else _EXCLUSIVE_LOCK_FLAGS

    def hold(self) -> _HeldPersistenceLock:
        """Build a context that acquires only when it is entered."""
        return _HeldPersistenceLock(self)

    def _acquire(self) -> IO[bytes]:
        sidecar = self._filesystem._open_lock_sidecar()
        try:
            deadline = self._monotonic() + self._timeout_seconds
            _wait_for_lock(
                sidecar,
                deadline,
                self._monotonic,
                self._sleep,
                self._flags,
            )
        except StoreLockedError:
            if not _close_sidecar(sidecar):
                raise LockUnavailableError(handle_closed=False) from None
            raise
        except portalocker.LockException, OSError:
            handle_closed = _close_sidecar(sidecar)
            raise LockUnavailableError(handle_closed=handle_closed) from None
        except BaseException as error:
            if not _close_sidecar(sidecar):
                error.add_note("Persistence lock handle cleanup also failed.")
            raise
        try:
            self._filesystem._prove_lock_sidecar_identity(sidecar)
        except BaseException as error:
            _release_after_invalidation(sidecar, error)
        return sidecar
