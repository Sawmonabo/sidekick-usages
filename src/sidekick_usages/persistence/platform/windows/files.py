"""Windows handle-qualified file operations."""

import os
import stat
import sys
from pathlib import Path

from sidekick_usages.persistence.platform.contracts import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32file
    import winerror

    from sidekick_usages.persistence.platform.windows.handles import (
        close_descriptor,
        close_handle,
        descriptor_from_handle,
        metadata,
        open_delete_target,
        open_mutation_source,
        owned_descriptor,
        owned_handle,
        read_descriptor,
        validate_stat,
        write_handle,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        child_path,
        path_attributes,
        require_exact_entry,
        validate_membership,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        private_security_attributes,
        validate_external_private_source_file,
        validate_external_source_directory,
        validate_security,
    )

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

    def open_directory(path: Path, *, private: bool = True) -> int:
        """Open and validate one exact non-reparse directory."""
        try:
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_READ,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_BACKUP_SEMANTICS
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.UNSAFE) from None
        try:
            if private:
                validate_security(int(handle), directory=True)
        except BaseException as error:
            close_handle(handle, error)
        descriptor = descriptor_from_handle(
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            validate_stat(
                metadata(descriptor, NativeFailureKind.UNSAFE),
                directory=True,
            )
            return descriptor
        except NativeFilesystemError as error:
            close_descriptor(descriptor, error)

    def open_external_source_directory(path: Path) -> int:
        """Open a non-reparse directory with bounded write authority."""
        descriptor = open_directory(path, private=False)
        try:
            validate_external_source_directory(
                msvcrt.get_osfhandle(descriptor)
            )
            return descriptor
        except BaseException as error:
            close_descriptor(descriptor, error)

    def open_existing(
        parent: Path,
        basename: str,
        parent_descriptor: int,
        *,
        writable: bool,
        external_source: bool = False,
    ) -> int:
        """Open one exact protected child and prove parent membership."""
        if not require_exact_entry(parent, basename):
            raise _native_error(NativeFailureKind.CHANGED)
        path = child_path(parent, basename)
        access = win32file.GENERIC_READ
        flags = os.O_RDONLY | os.O_BINARY
        if writable:
            access |= win32file.GENERIC_WRITE
            flags = os.O_RDWR | os.O_BINARY
        try:
            handle = win32file.CreateFile(
                str(path),
                access,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_ATTRIBUTE_NORMAL
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error as error:
            if error.winerror in (
                winerror.ERROR_FILE_NOT_FOUND,
                winerror.ERROR_PATH_NOT_FOUND,
            ):
                raise _native_error(NativeFailureKind.CHANGED) from None
            kind = (
                NativeFailureKind.UNSAFE
                if error.winerror == winerror.ERROR_ACCESS_DENIED
                else NativeFailureKind.UNREADABLE
            )
            raise _native_error(kind) from None
        try:
            if external_source:
                validate_external_private_source_file(int(handle))
            else:
                validate_security(int(handle), directory=False)
            validate_membership(parent_descriptor, int(handle), basename)
        except BaseException as error:
            close_handle(handle, error)
        descriptor = descriptor_from_handle(handle, flags)
        try:
            validate_stat(
                metadata(descriptor, NativeFailureKind.UNREADABLE),
                directory=False,
                directory_device=metadata(
                    parent_descriptor,
                    NativeFailureKind.UNREADABLE,
                ).st_dev,
                allow_interrupted_link=not external_source,
            )
            return descriptor
        except NativeFilesystemError as error:
            close_descriptor(descriptor, error)

    def _flush_path(
        parent: Path,
        basename: str,
        parent_descriptor: int,
    ) -> None:
        descriptor = open_existing(
            parent,
            basename,
            parent_descriptor,
            writable=True,
        )
        with owned_descriptor(descriptor, NativeFailureKind.HARDEN):
            try:
                win32file.FlushFileBuffers(msvcrt.get_osfhandle(descriptor))
            except OSError, pywintypes.error:
                raise _native_error(NativeFailureKind.HARDEN) from None

    def read_file(
        parent: Path,
        basename: str,
        limit: int,
        *,
        external_source: bool = False,
    ) -> NativeFile | None:
        """Read one bounded protected non-reparse sibling."""
        attributes = path_attributes(parent)
        if attributes is None:
            return None
        if (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        directory_descriptor = (
            open_external_source_directory(parent)
            if external_source
            else open_directory(parent)
        )
        with owned_descriptor(
            directory_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            if not require_exact_entry(parent, basename):
                return None
            file_descriptor = open_existing(
                parent,
                basename,
                directory_descriptor,
                writable=False,
                external_source=external_source,
            )
            with owned_descriptor(
                file_descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                result = read_descriptor(
                    file_descriptor,
                    metadata(
                        directory_descriptor,
                        NativeFailureKind.UNREADABLE,
                    ).st_dev,
                    limit,
                    allow_interrupted_link=not external_source,
                )
                try:
                    child_handle = msvcrt.get_osfhandle(file_descriptor)
                except OSError:
                    raise _native_error(NativeFailureKind.UNREADABLE) from None
                validate_membership(
                    directory_descriptor,
                    child_handle,
                    basename,
                )
                if not require_exact_entry(parent, basename):
                    raise _native_error(NativeFailureKind.CHANGED)
                return result

    def create_private_file(
        parent: Path,
        basename: str,
        data: bytes,
    ) -> NativeFile:
        """Create a private write-through file and verify it."""
        path = child_path(parent, basename)
        parent_descriptor = open_directory(parent)
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.CREATE,
        ):
            try:
                handle = win32file.CreateFile(
                    str(path),
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    private_security_attributes(directory=False),
                    win32file.CREATE_NEW,
                    win32file.FILE_ATTRIBUTE_NORMAL
                    | win32file.FILE_FLAG_WRITE_THROUGH
                    | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            except pywintypes.error as error:
                if error.winerror in (
                    winerror.ERROR_FILE_EXISTS,
                    winerror.ERROR_ALREADY_EXISTS,
                ):
                    raise _native_error(NativeFailureKind.EXISTS) from None
                raise _native_error(NativeFailureKind.CREATE) from None
            with owned_handle(handle):
                validate_security(int(handle), directory=False)
                validate_membership(
                    parent_descriptor,
                    int(handle),
                    basename,
                )
                write_handle(int(handle), data)
            reopened = read_file(parent, basename, len(data))
            if reopened is None or reopened.data != data:
                raise _native_error(NativeFailureKind.WRITE)
            return reopened

    def publish_no_replace(
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        device: int,
        inode: int,
    ) -> None:
        """Publish by validated no-replace write-through move."""
        source = child_path(parent, temporary_basename)
        destination = child_path(parent, final_basename)
        descriptor = open_directory(parent)
        with owned_descriptor(descriptor, NativeFailureKind.PUBLISH):
            source_descriptor = open_mutation_source(
                parent,
                temporary_basename,
                descriptor,
                device,
                inode,
            )
            with owned_descriptor(
                source_descriptor,
                NativeFailureKind.PUBLISH,
            ):
                try:
                    win32file.MoveFileExW(
                        str(source),
                        str(destination),
                        win32file.MOVEFILE_WRITE_THROUGH,
                    )
                except pywintypes.error as error:
                    if error.winerror in {
                        winerror.ERROR_FILE_EXISTS,
                        winerror.ERROR_ALREADY_EXISTS,
                    }:
                        raise _native_error(NativeFailureKind.EXISTS) from None
                    raise _native_error(NativeFailureKind.PUBLISH) from None
                source_handle = msvcrt.get_osfhandle(source_descriptor)
                validate_membership(
                    descriptor,
                    source_handle,
                    final_basename,
                )

    def replace_file(
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        *,
        destination_exists: bool,
        device: int,
        inode: int,
    ) -> None:
        """Replace authority through the approved Windows primitives."""
        source = child_path(parent, temporary_basename)
        destination = child_path(parent, final_basename)
        descriptor = open_directory(parent)
        with owned_descriptor(descriptor, NativeFailureKind.REPLACE):
            if destination_exists and not require_exact_entry(
                parent,
                final_basename,
            ):
                raise _native_error(NativeFailureKind.CHANGED)
            source_descriptor = open_mutation_source(
                parent,
                temporary_basename,
                descriptor,
                device,
                inode,
            )
            with owned_descriptor(
                source_descriptor,
                NativeFailureKind.REPLACE,
            ):
                try:
                    flags = win32file.MOVEFILE_WRITE_THROUGH
                    if destination_exists:
                        flags |= win32file.MOVEFILE_REPLACE_EXISTING
                    win32file.MoveFileExW(
                        str(source),
                        str(destination),
                        flags,
                    )
                except pywintypes.error as error:
                    if not destination_exists and error.winerror in {
                        winerror.ERROR_FILE_EXISTS,
                        winerror.ERROR_ALREADY_EXISTS,
                    }:
                        raise _native_error(NativeFailureKind.EXISTS) from None
                    raise _native_error(NativeFailureKind.REPLACE) from None
                source_handle = msvcrt.get_osfhandle(source_descriptor)
                validate_membership(
                    descriptor,
                    source_handle,
                    final_basename,
                )

    def harden_file(
        parent: Path,
        final_basename: str,
        limit: int,
    ) -> None:
        """Flush and security-check the final non-reparse object."""
        descriptor = open_directory(parent)
        with owned_descriptor(descriptor, NativeFailureKind.HARDEN):
            _flush_path(parent, final_basename, descriptor)
            if read_file(parent, final_basename, limit) is None:
                raise _native_error(NativeFailureKind.HARDEN)

    def remove_candidate(parent: Path, basename: str) -> bool:
        """Delete one exact owned temporary when present."""
        parent_descriptor = open_directory(parent)
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.REMOVE,
        ):
            if not require_exact_entry(parent, basename):
                return False
            descriptor = open_delete_target(
                parent,
                basename,
                parent_descriptor,
            )
            try:
                handle = msvcrt.get_osfhandle(descriptor)
                win32file.SetFileInformationByHandle(
                    handle,
                    win32file.FileDispositionInfo,
                    True,
                )
            except OSError, pywintypes.error:
                close_descriptor(
                    descriptor,
                    _native_error(NativeFailureKind.REMOVE),
                )
            close_descriptor(descriptor)
            if require_exact_entry(parent, basename):
                raise _native_error(NativeFailureKind.CHANGED)
            return True

    def remove_validated(
        parent: Path,
        basename: str,
        device: int,
        inode: int,
    ) -> bool:
        """Remove only the exact previously validated identity."""
        parent_descriptor = open_directory(parent)
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.REMOVE,
        ):
            if not require_exact_entry(parent, basename):
                return False
            descriptor = open_delete_target(
                parent,
                basename,
                parent_descriptor,
            )
            with owned_descriptor(
                descriptor,
                NativeFailureKind.REMOVE,
            ):
                file_metadata = metadata(
                    descriptor,
                    NativeFailureKind.REMOVE,
                )
                if (file_metadata.st_dev, file_metadata.st_ino) != (
                    device,
                    inode,
                ):
                    raise _native_error(NativeFailureKind.CHANGED)
                try:
                    handle = msvcrt.get_osfhandle(descriptor)
                    win32file.SetFileInformationByHandle(
                        handle,
                        win32file.FileDispositionInfo,
                        True,
                    )
                except OSError, pywintypes.error:
                    raise _native_error(NativeFailureKind.REMOVE) from None
            if require_exact_entry(parent, basename):
                raise _native_error(NativeFailureKind.CHANGED)
            return True


__all__ = [
    "create_private_file",
    "harden_file",
    "open_directory",
    "open_existing",
    "open_external_source_directory",
    "publish_no_replace",
    "read_file",
    "remove_candidate",
    "remove_validated",
    "replace_file",
]
