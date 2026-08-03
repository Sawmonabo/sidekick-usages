"""Stable owner-qualified storage for shell integration files."""

import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_MAXIMUM_SHELL_FILE_BYTES = 1024 * 1024
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class ShellPersistenceError(OSError):
    """A shell file failed stable owner-qualified access."""


@dataclass(frozen=True, slots=True)
class ShellFileSnapshot:
    """Exact stable shell file bytes and filesystem identity."""

    data: bytes
    device: int
    inode: int
    modified_nanoseconds: int
    mode: int


class ShellFileStore:
    """Compare-and-swap shell files beneath one trusted user home."""

    def __init__(self, home: Path, effective_user_id: int) -> None:
        if not home.is_absolute() or effective_user_id < 0:
            raise ValueError("Shell persistence root is invalid.")
        self._home = home
        self._effective_user_id = effective_user_id

    def read(self, path: Path) -> ShellFileSnapshot | None:
        """Return one bounded stable owner-qualified file snapshot."""
        self._require_target(path)
        self._require_existing_ancestors(path.parent)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ShellPersistenceError from error
        self._require_regular_owner(before)
        if before.st_size > _MAXIMUM_SHELL_FILE_BYTES:
            raise ShellPersistenceError
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            self._require_same_file(before, opened)
            chunks: list[bytes] = []
            remaining = _MAXIMUM_SHELL_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        except OSError as error:
            raise ShellPersistenceError from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        data = b"".join(chunks)
        if len(data) > _MAXIMUM_SHELL_FILE_BYTES:
            raise ShellPersistenceError
        self._require_same_file(before, after)
        return ShellFileSnapshot(
            data=data,
            device=before.st_dev,
            inode=before.st_ino,
            modified_nanoseconds=before.st_mtime_ns,
            mode=stat.S_IMODE(before.st_mode),
        )

    def write(
        self,
        path: Path,
        expected: ShellFileSnapshot | None,
        payload: bytes,
        *,
        owner_only: bool,
    ) -> ShellFileSnapshot:
        """Atomically replace one unchanged target and prove publication."""
        if len(payload) > _MAXIMUM_SHELL_FILE_BYTES:
            raise ShellPersistenceError
        parent = self._prepare_parent(path)
        mode = (
            _PRIVATE_FILE_MODE
            if owner_only or expected is None
            else expected.mode
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                dir=parent,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, mode)
            pending = memoryview(payload)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError
                pending = pending[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._require_expected(path, expected)
            os.replace(temporary, path)
            temporary = None
            published = self.read(path)
            if published is None or published.data != payload:
                raise OSError
            if owner_only and published.mode != _PRIVATE_FILE_MODE:
                raise OSError
            self._sync_directory(parent)
            return published
        except OSError as error:
            raise ShellPersistenceError from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def remove(
        self,
        path: Path,
        expected: ShellFileSnapshot,
    ) -> None:
        """Remove only one byte- and identity-matching shell file."""
        try:
            self._require_expected(path, expected)
            path.unlink()
            self._sync_directory(path.parent)
        except OSError as error:
            raise ShellPersistenceError from error

    def _prepare_parent(self, path: Path) -> Path:
        self._require_target(path)
        self._ensure_root()
        current = self._home
        for component in path.parent.relative_to(self._home).parts:
            current /= component
            with suppress(FileExistsError):
                current.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            self._require_directory_owner(current)
        return path.parent

    def _ensure_root(self) -> None:
        missing: list[Path] = []
        current = self._home
        while True:
            try:
                self._require_directory_owner(current)
                break
            except FileNotFoundError:
                missing.append(current)
                if current.parent == current:
                    raise ShellPersistenceError from None
                current = current.parent
        for path in reversed(missing):
            try:
                path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            except OSError as error:
                raise ShellPersistenceError from error
            self._require_directory_owner(path)

    def _require_existing_ancestors(self, parent: Path) -> None:
        try:
            parts = parent.relative_to(self._home).parts
        except ValueError:
            raise ShellPersistenceError from None
        current = self._home
        try:
            self._require_directory_owner(current)
            for component in parts:
                current /= component
                self._require_directory_owner(current)
        except FileNotFoundError:
            return

    def _require_target(self, path: Path) -> None:
        if not path.is_absolute() or path == self._home:
            raise ShellPersistenceError
        try:
            path.relative_to(self._home)
        except ValueError:
            raise ShellPersistenceError from None

    def _require_expected(
        self,
        path: Path,
        expected: ShellFileSnapshot | None,
    ) -> None:
        current = self.read(path)
        if current != expected:
            raise ShellPersistenceError

    def _require_directory_owner(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise
        except OSError as error:
            raise ShellPersistenceError from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._effective_user_id
        ):
            raise ShellPersistenceError

    def _require_regular_owner(self, metadata: os.stat_result) -> None:
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._effective_user_id
            or metadata.st_nlink != 1
        ):
            raise ShellPersistenceError

    @staticmethod
    def _require_same_file(
        before: os.stat_result,
        after: os.stat_result,
    ) -> None:
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
        ):
            raise ShellPersistenceError

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
