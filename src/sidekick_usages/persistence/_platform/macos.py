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
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/stat",
                        "-f",
                        "%T",
                        f"/dev/fd/{descriptor}",
                    ],
                    check=False,
                    capture_output=True,
                    encoding="utf-8",
                    pass_fds=(descriptor,),
                    timeout=_FILESYSTEM_REPORT_TIMEOUT_SECONDS,
                )
            except OSError, subprocess.SubprocessError:
                raise NativeFilesystemError(
                    NativeFailureKind.UNSUPPORTED
                ) from None
            if result.returncode != 0 or result.stdout.strip() != "apfs":
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
