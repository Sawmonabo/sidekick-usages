"""APFS persistence adapter with macOS full-file synchronization."""

import ctypes
import fcntl
import os
from pathlib import Path

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.macos.acl import has_extended_acl
from sidekick_usages.persistence.platform.posix import namespace
from sidekick_usages.persistence.platform.posix.adapter import PosixPlatform
from sidekick_usages.persistence.platform.types import (
    FilesystemFamily,
    NativeFailureKind,
)

_FILESYSTEM_TYPE_NAME_BYTES = 16
_MOUNT_PATH_BYTES = 1024
try:
    _SYSTEM_LIBRARY = ctypes.CDLL(None, use_errno=True)
    _FSTATFS64 = _SYSTEM_LIBRARY.fstatfs64
except AttributeError, OSError:
    _SYSTEM_LIBRARY = None
    _FSTATFS64 = None
else:
    _FSTATFS64.argtypes = [ctypes.c_int, ctypes.c_void_p]
    _FSTATFS64.restype = ctypes.c_int


class _DarwinFilesystemReport(ctypes.Structure):
    """Darwin ``statfs64`` layout from ``sys/mount.h``."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * _FILESYSTEM_TYPE_NAME_BYTES),
        ("f_mntonname", ctypes.c_char * _MOUNT_PATH_BYTES),
        ("f_mntfromname", ctypes.c_char * _MOUNT_PATH_BYTES),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def _filesystem_name(descriptor: int) -> str:
    """Return the native filesystem name for one held descriptor."""
    operation = _FSTATFS64
    if operation is None:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED) from None
    report = _DarwinFilesystemReport()
    status = operation(descriptor, ctypes.byref(report))
    if status != 0:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    try:
        return bytes(report.f_fstypename).decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED) from None


class MacOSPlatform(PosixPlatform):
    """macOS adapter requiring APFS and ``F_FULLFSYNC`` support."""

    def qualify(self, parent: Path) -> FilesystemFamily:
        """Require APFS for the securely opened actual directory."""
        ancestor = namespace.existing_ancestor(parent)
        descriptor = namespace.open_directory(ancestor, private=False)
        with namespace.owned_descriptor(
            descriptor,
            NativeFailureKind.UNSUPPORTED,
        ):
            if _filesystem_name(descriptor) != "apfs":
                raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
            return FilesystemFamily.APFS

    def _qualify_provider_descriptor(self, descriptor: int) -> None:
        """Require APFS on the held provider directory."""
        if _filesystem_name(descriptor) != "apfs":
            raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)

    def _validate_provider_descriptor(self, descriptor: int) -> None:
        """Reject any extended ACL on a provider directory or file."""
        try:
            if has_extended_acl(descriptor):
                raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None

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
