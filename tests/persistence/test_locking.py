"""Load-bearing tests for lock ownership and transaction lifetime."""

import io
import multiprocessing
import os
import threading
from multiprocessing.connection import Connection
from pathlib import Path
from typing import IO

import portalocker
import pytest

import sidekick_usages.persistence.locking
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import (
    LOCK_CHECK_INTERVAL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    PersistenceLock,
    StoreLockedError,
    TransactionReleaseError,
)
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.error import PersistenceCode


class InjectedFailureError(Exception):
    """Test-only failure with stable identity."""


class FakeClock:
    """Deterministic monotonic clock advanced only by injected sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _hold_lock_in_process(path: str, connection: Connection) -> None:
    filesystem = PersistenceFilesystem(Path(path))
    with PersistenceLock(filesystem).hold():
        connection.send("locked")
        connection.recv()
    connection.close()


def _filesystem(tmp_path: Path) -> PersistenceFilesystem:
    return PersistenceFilesystem(tmp_path / "state" / "accounts.json")


def test_hold_is_side_effect_free_until_context_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    opens = 0

    def open_sidecar() -> io.BytesIO:
        nonlocal opens
        opens += 1
        return sidecar

    monkeypatch.setattr(filesystem, "_open_lock_sidecar", open_sidecar)
    monkeypatch.setattr(
        filesystem,
        "_prove_lock_sidecar_identity",
        lambda _sidecar: None,
    )
    monkeypatch.setattr(portalocker, "lock", lambda *_args: None)
    monkeypatch.setattr(portalocker, "unlock", lambda *_args: None)

    context = PersistenceLock(filesystem).hold()
    assert opens == 0
    with context:
        assert opens == 1
        assert not sidecar.closed
    assert sidecar.closed


def test_timeout_uses_exact_policy_and_closes_the_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    clock = FakeClock()
    monkeypatch.setattr(filesystem, "_open_lock_sidecar", lambda: sidecar)

    def contend(*_args: object) -> None:
        raise portalocker.AlreadyLocked

    monkeypatch.setattr(portalocker, "lock", contend)

    with (
        pytest.raises(StoreLockedError) as exc_info,
        PersistenceLock(
            filesystem,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ).hold(),
    ):
        raise AssertionError("timeout must happen before context entry")

    assert exc_info.value.code is PersistenceCode.STORE_LOCKED
    assert clock.now == LOCK_TIMEOUT_SECONDS
    assert clock.waits
    assert max(clock.waits) <= LOCK_CHECK_INTERVAL_SECONDS
    assert sidecar.closed


def test_unknown_retry_failure_preserves_error_and_closes_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    clock = FakeClock()
    expected = InjectedFailureError("sleep interrupted")
    monkeypatch.setattr(filesystem, "_open_lock_sidecar", lambda: sidecar)
    monkeypatch.setattr(
        portalocker,
        "lock",
        lambda *_args: (_ for _ in ()).throw(portalocker.AlreadyLocked()),
    )

    def fail_sleep(_seconds: float) -> None:
        raise expected

    with (
        pytest.raises(InjectedFailureError) as exc_info,
        PersistenceLock(
            filesystem,
            monotonic=clock.monotonic,
            sleep=fail_sleep,
        ).hold(),
    ):
        raise AssertionError("retry failure must prevent context entry")

    assert exc_info.value is expected
    assert sidecar.closed


def test_transaction_construction_failure_releases_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    expected = InjectedFailureError("transaction construction failed")
    unlocked: list[io.BytesIO] = []
    monkeypatch.setattr(filesystem, "_open_lock_sidecar", lambda: sidecar)
    monkeypatch.setattr(
        filesystem,
        "_prove_lock_sidecar_identity",
        lambda _sidecar: None,
    )
    monkeypatch.setattr(portalocker, "lock", lambda *_args: None)

    def record_unlock(stream: io.BytesIO) -> None:
        unlocked.append(stream)

    monkeypatch.setattr(portalocker, "unlock", record_unlock)
    monkeypatch.setattr(
        sidekick_usages.persistence.locking,
        "_begin_transaction",
        lambda _filesystem: (_ for _ in ()).throw(expected),
    )

    with (
        pytest.raises(InjectedFailureError) as exc_info,
        PersistenceLock(filesystem).hold(),
    ):
        raise AssertionError("construction failure must prevent entry")

    assert exc_info.value is expected
    assert unlocked == [sidecar]
    assert sidecar.closed


def test_release_failure_retains_owned_operation_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    monkeypatch.setattr(filesystem, "_open_lock_sidecar", lambda: sidecar)
    monkeypatch.setattr(
        filesystem,
        "_prove_lock_sidecar_identity",
        lambda _sidecar: None,
    )
    monkeypatch.setattr(portalocker, "lock", lambda *_args: None)
    monkeypatch.setattr(
        portalocker,
        "unlock",
        lambda *_args: (_ for _ in ()).throw(portalocker.LockException()),
    )

    with (
        pytest.raises(TransactionReleaseError) as exc_info,
        PersistenceLock(filesystem).hold(),
    ):
        raise DurabilityUncertainError("accounts.json")

    error = exc_info.value
    assert error.code is PersistenceCode.DURABILITY_UNCERTAIN
    assert error.artifact_basename == "accounts.json"
    assert error.handle_closed
    assert not error.lock_may_be_held


def test_real_process_contention_then_release(tmp_path: Path) -> None:
    filesystem = _filesystem(tmp_path)
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        target=_hold_lock_in_process,
        args=(str(filesystem.authority_path), child_connection),
    )
    process.start()
    child_connection.close()
    try:
        assert parent_connection.recv() == "locked"
        clock = FakeClock()
        with (
            pytest.raises(StoreLockedError),
            PersistenceLock(
                filesystem,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ).hold(),
        ):
            raise AssertionError("the child still owns the lock")
        parent_connection.send("release")
        process.join(timeout=10)
        assert process.exitcode == 0
        with PersistenceLock(filesystem).hold():
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent_connection.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics")
def test_replaced_sidecar_after_lock_acquisition_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    filesystem._prepare_parent()
    lock_path = filesystem.authority_path.with_name("accounts.json.lock")
    original_lock = portalocker.lock
    locked_stream: IO[bytes] | None = None

    def replace_after_lock(
        stream: IO[bytes],
        flags: portalocker.LockFlags,
    ) -> None:
        nonlocal locked_stream
        original_lock(stream, flags)
        locked_stream = stream
        replacement = lock_path.with_name("replacement.lock")
        replacement.write_bytes(b"replacement")
        replacement.chmod(0o600)
        os.replace(replacement, lock_path)

    monkeypatch.setattr(portalocker, "lock", replace_after_lock)

    with (
        pytest.raises(UnsafeManagedFileError) as exc_info,
        PersistenceLock(filesystem).hold(),
    ):
        raise AssertionError("identity failure must prevent context entry")

    assert exc_info.value.code is PersistenceCode.UNSAFE_PERMISSIONS
    assert exc_info.value.artifact_basename == "accounts.json.lock"
    assert locked_stream is not None
    assert locked_stream.closed
    assert lock_path.read_bytes() == b"replacement"


def test_context_exit_waits_for_inflight_operation_and_invalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    sidecar = io.BytesIO()
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    monkeypatch.setattr(filesystem, "_open_lock_sidecar", lambda: sidecar)
    monkeypatch.setattr(
        filesystem,
        "_prove_lock_sidecar_identity",
        lambda _sidecar: None,
    )
    monkeypatch.setattr(portalocker, "lock", lambda *_args: None)
    monkeypatch.setattr(portalocker, "unlock", lambda *_args: None)

    def blocking_commit(
        _payload: bytes,
        _expected: AuthorityExpectation,
    ) -> FileSnapshot:
        started.set()
        assert release.wait(timeout=5)
        raise InjectedFailureError

    monkeypatch.setattr(filesystem, "_commit_authority", blocking_commit)
    context = PersistenceLock(filesystem).hold()
    transaction = context.__enter__()

    def commit_authority() -> None:
        try:
            transaction.commit_authority(
                b"test-only-payload",
                AuthorityExpectation.ABSENT,
            )
        except InjectedFailureError:
            return

    worker = threading.Thread(target=commit_authority)
    worker.start()
    assert started.wait(timeout=5)

    def exit_context() -> None:
        context.__exit__(None, None, None)
        exited.set()

    exiting = threading.Thread(target=exit_context)
    exiting.start()
    assert not exited.wait(timeout=0.05)
    release.set()
    worker.join(timeout=5)
    exiting.join(timeout=5)

    assert exited.is_set()
    with pytest.raises(RuntimeError, match="no longer active"):
        transaction.commit_authority(
            b"test-only-payload",
            AuthorityExpectation.ABSENT,
        )
