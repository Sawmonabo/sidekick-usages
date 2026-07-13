"""Windows handle ownership, metadata, and bounded I/O."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)

if TYPE_CHECKING and sys.platform == "win32":
    import _win32typing

_READ_CHUNK_BYTES = 64 * 1024


if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32api
    import win32con
    import win32file

    from sidekick_usages.persistence._platform.windows_namespace import (
        child_path,
        require_exact_entry,
        validate_membership,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        validate_security,
    )

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

    def close_descriptor(
        descriptor: int,
        primary: BaseException | None = None,
    ) -> None:
        """Close an OS descriptor without replacing an owned failure."""
        try:
            os.close(descriptor)
        except OSError:
            if primary is None:
                raise _native_error(NativeFailureKind.UNSAFE) from None
            primary.add_note("Native descriptor cleanup also failed.")
        if primary is not None:
            raise primary from None

    @contextmanager
    def owned_descriptor(
        descriptor: int,
        failure_kind: NativeFailureKind,
    ) -> Iterator[int]:
        """Own one descriptor and translate its complete operation."""
        primary: BaseException | None = None
        try:
            yield descriptor
        except NativeFilesystemError as error:
            primary = error
        except OSError:
            primary = _native_error(failure_kind)
        except BaseException as error:
            primary = error
        try:
            os.close(descriptor)
        except OSError:
            if primary is not None:
                primary.add_note("Native descriptor cleanup also failed.")
            else:
                primary = _native_error(NativeFailureKind.UNSAFE)
        if primary is not None:
            raise primary from None

    def metadata(
        descriptor: int,
        failure_kind: NativeFailureKind,
    ) -> os.stat_result:
        """Return descriptor metadata or one owned native failure."""
        try:
            return os.fstat(descriptor)
        except OSError:
            raise _native_error(failure_kind) from None

    def close_handle(
        handle: _win32typing.PyHANDLE,
        primary: BaseException | None = None,
    ) -> None:
        """Close one pywin32 handle while preserving an owned failure."""
        try:
            handle.Close()
        except pywintypes.error:
            if primary is None:
                raise _native_error(NativeFailureKind.UNSAFE) from None
            primary.add_note("Native handle cleanup also failed.")
        if primary is not None:
            raise primary from None

    @contextmanager
    def owned_handle(
        handle: _win32typing.PyHANDLE,
    ) -> Iterator[_win32typing.PyHANDLE]:
        """Own one pywin32 handle until deliberate descriptor transfer."""
        primary: BaseException | None = None
        try:
            yield handle
        except BaseException as error:
            primary = error
        close_handle(handle, primary)

    def _close_raw_handle(
        handle: int,
        primary: BaseException | None = None,
    ) -> None:
        try:
            win32api.CloseHandle(handle)
        except pywintypes.error:
            if primary is None:
                raise _native_error(NativeFailureKind.UNSAFE) from None
            primary.add_note("Native handle cleanup also failed.")
        if primary is not None:
            raise primary from None

    def descriptor_from_handle(
        handle: _win32typing.PyHANDLE,
        flags: int,
    ) -> int:
        """Transfer one pywin32 handle into Python descriptor ownership."""
        raw_handle: int | None = None
        try:
            raw_handle = int(handle.Detach())
            return msvcrt.open_osfhandle(raw_handle, flags)
        except OSError, ValueError, pywintypes.error:
            error = _native_error(NativeFailureKind.UNSAFE)
            if raw_handle is None:
                close_handle(handle, error)
            else:
                _close_raw_handle(raw_handle, error)
            raise error from None
        except BaseException as error:
            if raw_handle is None:
                close_handle(handle, error)
            else:
                _close_raw_handle(raw_handle, error)
            raise

    def validate_stat(
        value: os.stat_result,
        *,
        directory: bool,
        directory_device: int | None = None,
        allow_interrupted_link: bool = False,
    ) -> None:
        """Reject reparse, type, link-count, and volume mismatches."""
        attributes = getattr(value, "st_file_attributes", 0)
        if type(attributes) is not int or (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        if directory:
            if not stat.S_ISDIR(value.st_mode):
                raise _native_error(NativeFailureKind.UNSAFE)
            return
        allowed_links = {1, 2} if allow_interrupted_link else {1}
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink not in allowed_links
            or (
                directory_device is not None
                and value.st_dev != directory_device
            )
        ):
            raise _native_error(NativeFailureKind.UNSAFE)

    def read_descriptor(
        descriptor: int,
        directory_device: int,
        limit: int,
        *,
        allow_interrupted_link: bool = True,
    ) -> NativeFile:
        """Bounded-read and revalidate one stable open file handle."""
        before = metadata(descriptor, NativeFailureKind.UNREADABLE)
        validate_stat(
            before,
            directory=False,
            directory_device=directory_device,
            allow_interrupted_link=allow_interrupted_link,
        )
        if before.st_size > limit:
            raise _native_error(NativeFailureKind.TOO_LARGE)
        chunks: list[bytes] = []
        remaining = 0 if before.st_size == 0 else limit + 1
        try:
            while remaining:
                chunk = os.read(
                    descriptor,
                    min(_READ_CHUNK_BYTES, remaining),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        data = b"".join(chunks)
        if len(data) > limit:
            raise _native_error(NativeFailureKind.TOO_LARGE)
        after = metadata(descriptor, NativeFailureKind.UNREADABLE)
        try:
            handle = msvcrt.get_osfhandle(descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        validate_security(handle, directory=False)
        validate_stat(
            after,
            directory=False,
            directory_device=directory_device,
            allow_interrupted_link=allow_interrupted_link,
        )
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or len(data) != after.st_size
        ):
            raise _native_error(NativeFailureKind.CHANGED)
        return NativeFile(
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            data,
        )

    def write_handle(handle: int, data: bytes) -> None:
        """Write and flush all bytes through one validated handle."""
        written = 0
        try:
            while written < len(data):
                _status, count = win32file.WriteFile(
                    handle,
                    data[written:],
                )
                if count <= 0:
                    raise _native_error(NativeFailureKind.WRITE)
                written += count
            win32file.FlushFileBuffers(handle)
        except pywintypes.error:
            raise _native_error(NativeFailureKind.WRITE) from None

    def open_mutation_source(
        parent: Path,
        basename: str,
        parent_descriptor: int,
        device: int,
        inode: int,
    ) -> int:
        """Open and identity-bind one candidate before native mutation."""
        if not require_exact_entry(parent, basename):
            raise _native_error(NativeFailureKind.CHANGED)
        try:
            handle = win32file.CreateFile(
                str(child_path(parent, basename)),
                win32file.GENERIC_READ
                | win32file.GENERIC_WRITE
                | win32con.DELETE,
                win32file.FILE_SHARE_READ
                | win32file.FILE_SHARE_WRITE
                | win32file.FILE_SHARE_DELETE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_ATTRIBUTE_NORMAL
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.CHANGED) from None
        try:
            validate_security(int(handle), directory=False)
            validate_membership(
                parent_descriptor,
                int(handle),
                basename,
            )
        except BaseException as error:
            close_handle(handle, error)
        descriptor = descriptor_from_handle(
            handle,
            os.O_RDWR | os.O_BINARY,
        )
        value = metadata(descriptor, NativeFailureKind.UNSAFE)
        if (value.st_dev, value.st_ino) != (device, inode):
            close_descriptor(
                descriptor,
                _native_error(NativeFailureKind.CHANGED),
            )
        return descriptor

    def open_delete_target(
        parent: Path,
        basename: str,
        parent_descriptor: int,
    ) -> int:
        """Open and membership-bind one exact deletion target."""
        if not require_exact_entry(parent, basename):
            raise _native_error(NativeFailureKind.CHANGED)
        try:
            handle = win32file.CreateFile(
                str(child_path(parent, basename)),
                win32file.GENERIC_READ | win32con.DELETE,
                win32file.FILE_SHARE_READ
                | win32file.FILE_SHARE_WRITE
                | win32file.FILE_SHARE_DELETE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_ATTRIBUTE_NORMAL
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.CHANGED) from None
        try:
            validate_security(int(handle), directory=False)
            validate_membership(
                parent_descriptor,
                int(handle),
                basename,
            )
        except BaseException as error:
            close_handle(handle, error)
        descriptor = descriptor_from_handle(
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            value = metadata(descriptor, NativeFailureKind.UNSAFE)
            parent_value = metadata(
                parent_descriptor,
                NativeFailureKind.UNSAFE,
            )
            validate_stat(
                value,
                directory=False,
                directory_device=parent_value.st_dev,
                allow_interrupted_link=False,
            )
        except BaseException as error:
            close_descriptor(descriptor, error)
        return descriptor


__all__ = [
    "close_descriptor",
    "close_handle",
    "descriptor_from_handle",
    "metadata",
    "open_delete_target",
    "open_mutation_source",
    "owned_descriptor",
    "owned_handle",
    "read_descriptor",
    "validate_stat",
    "write_handle",
]
