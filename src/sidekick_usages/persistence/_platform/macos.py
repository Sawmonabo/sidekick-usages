"""APFS persistence operations with macOS full-file synchronization."""

import fcntl
import os
import subprocess
from pathlib import Path

from sidekick_usages.persistence._platform import (
    FilesystemFamily,
    NativeFailureKind,
    NativeFilesystemError,
)
from sidekick_usages.persistence._platform.posix import (
    PosixPlatform,
    _existing_ancestor,
    _open_directory,
    _owned_descriptor,
)

_FILESYSTEM_REPORT_TIMEOUT_SECONDS = 5.0
_GETPATH_BUFFER_BYTES = 1024


def _descriptor_path(descriptor: int) -> Path:
    """Return the identity-proven path for one held macOS descriptor."""
    operation = getattr(fcntl, "F_GETPATH", None)
    if type(operation) is not int:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    try:
        payload = fcntl.fcntl(
            descriptor,
            operation,
            bytes(_GETPATH_BUFFER_BYTES),
        )
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED) from None
    if not isinstance(payload, bytes):
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    raw_path, _separator, _remainder = payload.partition(b"\0")
    if not raw_path:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    path = Path(os.fsdecode(raw_path))
    if not path.is_absolute():
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    return path


def _filesystem_name(descriptor: int) -> str:
    """Report the filesystem of the same object held by ``descriptor``."""
    path = _descriptor_path(descriptor)
    try:
        held_before = os.fstat(descriptor)
        named_before = os.stat(path, follow_symlinks=False)
        result = subprocess.run(
            ["/usr/bin/stat", "-f", "%T", os.fspath(path)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=_FILESYSTEM_REPORT_TIMEOUT_SECONDS,
        )
        held_after = os.fstat(descriptor)
        named_after = os.stat(path, follow_symlinks=False)
    except OSError, subprocess.SubprocessError:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED) from None
    identity = (held_before.st_dev, held_before.st_ino)
    if (
        identity != (named_before.st_dev, named_before.st_ino)
        or identity != (held_after.st_dev, held_after.st_ino)
        or identity != (named_after.st_dev, named_after.st_ino)
        or result.returncode != 0
    ):
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    return result.stdout.strip()


class MacOSPlatform(PosixPlatform):
    """macOS adapter requiring APFS and ``F_FULLFSYNC`` support."""

    def qualify(self, parent: Path) -> FilesystemFamily:
        """Require APFS for the securely opened actual directory."""
        ancestor = _existing_ancestor(parent)
        descriptor = _open_directory(ancestor, private=False)
        with _owned_descriptor(
            descriptor,
            NativeFailureKind.UNSUPPORTED,
        ):
            if _filesystem_name(descriptor) != "apfs":
                raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
            return FilesystemFamily.APFS

    def _synchronize_file(self, descriptor: int) -> None:
        """Request both kernel and drive-cache synchronization."""
        try:
            os.fsync(descriptor)
            full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
            if type(full_fsync) is not int:
                raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
            fcntl.fcntl(descriptor, full_fsync)
        except OSError:
            raise NativeFilesystemError(
                NativeFailureKind.SYNCHRONIZE
            ) from None


__all__ = ["MacOSPlatform"]
