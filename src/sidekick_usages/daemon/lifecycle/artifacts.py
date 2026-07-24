"""Atomic owner-only storage for generated service definitions."""

import os
import stat
import tempfile
from pathlib import Path

from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import ServiceArtifact
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class ServiceArtifactStore:
    """Write exact service artifacts below one trusted user home."""

    def __init__(self, home: Path, uid: int) -> None:
        if not home.is_absolute() or uid < 0:
            raise ValueError("Service artifact root is invalid.")
        self._home = home
        self._uid = uid

    def write(self, artifact: ServiceArtifact) -> None:
        """Atomically publish and verify one owner-only artifact."""
        descriptor = -1
        temporary: Path | None = None
        try:
            parent = self._prepare_parent(artifact.path)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{artifact.path.name}.",
                dir=parent,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, _FILE_MODE)
            pending = memoryview(artifact.payload)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError
                pending = pending[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._require_regular_owner(temporary, _FILE_MODE)
            self._require_replaceable(artifact.path)
            os.replace(temporary, artifact.path)
            self._require_regular_owner(artifact.path, artifact.mode)
            self._sync_directory(parent)
        except OSError, ValueError:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None

    def ensure_directory(self, path: Path) -> None:
        """Create and validate one owner-only directory below the home."""
        try:
            self._prepare_parent(path / ".directory-boundary")
            if stat.S_IMODE(path.stat().st_mode) != _DIRECTORY_MODE:
                os.chmod(path, _DIRECTORY_MODE)
                self._require_directory_owner(path)
        except OSError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None

    def exists(self, path: Path) -> bool:
        """Return whether one exact safe artifact exists."""
        try:
            self._require_below_home(path)
            self._require_regular_owner(path, _FILE_MODE)
        except FileNotFoundError:
            return False
        except OSError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None
        return True

    def delete(self, path: Path) -> None:
        """Delete one exact safe artifact when present."""
        try:
            self._require_below_home(path)
            self._require_regular_owner(path, _FILE_MODE)
        except FileNotFoundError:
            return
        except OSError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None
        try:
            path.unlink()
            self._sync_directory(path.parent)
        except OSError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None

    def _prepare_parent(self, path: Path) -> Path:
        self._require_below_home(path)
        self._home.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        self._require_directory_owner(self._home)
        current = self._home
        for component in path.parent.relative_to(self._home).parts:
            current /= component
            current.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
            self._require_directory_owner(current)
        return current

    def _require_below_home(self, path: Path) -> None:
        if not path.is_absolute() or path == self._home:
            raise ValueError("Service artifact path is invalid.")
        try:
            path.relative_to(self._home)
        except ValueError:
            raise ValueError(
                "Service artifact escapes the user home."
            ) from None

    def _require_directory_owner(self, path: Path) -> None:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._uid
        ):
            raise OSError

    def _require_regular_owner(self, path: Path, mode: int) -> None:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._uid
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise OSError

    def _require_replaceable(self, path: Path) -> None:
        try:
            self._require_regular_owner(path, _FILE_MODE)
        except FileNotFoundError:
            return

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
