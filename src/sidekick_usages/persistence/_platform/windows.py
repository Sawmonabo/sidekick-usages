"""NTFS persistence operations implemented through pywin32."""

import os
import stat
import sys
from pathlib import Path
from typing import IO

from sidekick_usages.persistence._platform import (
    FilesystemFamily,
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32con
    import win32file
    import winerror

    from sidekick_usages.persistence._platform import windows_files
    from sidekick_usages.persistence._platform.windows_files import (
        create_private_file as _create_private_file,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        harden_file as _harden_file,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        open_directory as _open_directory,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        publish_no_replace as _publish_no_replace,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        read_file as _read_file,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        remove_candidate as _remove_candidate,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        remove_validated as _remove_validated,
    )
    from sidekick_usages.persistence._platform.windows_files import (
        replace_file as _replace_file,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        close_descriptor as _close_descriptor,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        close_handle as _close_handle,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        descriptor_from_handle as _descriptor_from_handle,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        metadata as _metadata,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        owned_descriptor as _owned_descriptor,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        validate_stat as _validate_stat,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        child_path as _child,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        existing_ancestor as _existing_ancestor,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        path_attributes as _path_attributes,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        qualify_local_ntfs as _qualify_local_ntfs,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        require_exact_entry as _require_exact_entry,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        validate_membership as _validate_membership,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        private_security_attributes as _private_security_attributes,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        repair_security as _repair_security,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        validate_repair_owner as _validate_repair_owner,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        validate_security as _validate_security,
    )

    _open_existing = windows_files.open_existing

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

    def _open_security_repair_directory(path: Path) -> int:
        try:
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_READ
                | win32con.READ_CONTROL
                | win32con.WRITE_DAC,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_BACKUP_SEMANTICS
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.UNSAFE) from None
        descriptor = _descriptor_from_handle(
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            _validate_stat(
                _metadata(descriptor, NativeFailureKind.UNSAFE),
                directory=True,
            )
            return descriptor
        except NativeFilesystemError as error:
            _close_descriptor(descriptor, error)

    def _close_descriptor_stack(
        descriptors: list[int],
        primary: NativeFilesystemError | None = None,
    ) -> None:
        failure = primary
        for descriptor in reversed(descriptors):
            try:
                _close_descriptor(descriptor)
            except NativeFilesystemError as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Native descriptor cleanup also failed.")
        if failure is not None:
            raise failure from None

    def _open_or_create_directory(path: Path, *, private: bool) -> int:
        attributes = _path_attributes(path)
        if attributes is None:
            try:
                win32file.CreateDirectoryW(
                    str(path),
                    _private_security_attributes(directory=True),
                )
            except pywintypes.error as error:
                if error.winerror != winerror.ERROR_ALREADY_EXISTS:
                    raise _native_error(NativeFailureKind.CREATE) from None
        elif (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        return _open_directory(path, private=private)

    def _ensure_private_parent(parent: Path) -> None:
        ancestor = _existing_ancestor(parent)
        components = parent.relative_to(ancestor).parts
        descriptors = [_open_directory(ancestor, private=False)]
        current = ancestor
        try:
            for index, component in enumerate(components):
                current /= component
                descriptors.append(
                    _open_or_create_directory(
                        current,
                        private=index == len(components) - 1,
                    )
                )
            if not components:
                _validate_security(
                    msvcrt.get_osfhandle(descriptors[-1]),
                    directory=True,
                )
        except OSError:
            _close_descriptor_stack(
                descriptors,
                _native_error(NativeFailureKind.UNSAFE),
            )
        except NativeFilesystemError as error:
            _close_descriptor_stack(descriptors, error)
        _close_descriptor_stack(descriptors)

    def _open_lock_child(
        parent: Path,
        basename: str,
        parent_descriptor: int,
    ) -> IO[bytes]:
        path = _child(parent, basename)
        created = False
        try:
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                _private_security_attributes(directory=False),
                win32file.CREATE_NEW,
                win32file.FILE_ATTRIBUTE_NORMAL
                | win32file.FILE_FLAG_WRITE_THROUGH
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            created = True
        except pywintypes.error as error:
            if error.winerror not in (
                winerror.ERROR_FILE_EXISTS,
                winerror.ERROR_ALREADY_EXISTS,
            ):
                raise _native_error(NativeFailureKind.CREATE) from None
            if not _require_exact_entry(parent, basename):
                raise _native_error(NativeFailureKind.UNSAFE) from None
            try:
                handle = win32file.CreateFile(
                    str(path),
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                    None,
                    win32file.OPEN_EXISTING,
                    win32file.FILE_ATTRIBUTE_NORMAL
                    | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            except pywintypes.error:
                raise _native_error(NativeFailureKind.UNSAFE) from None
        try:
            _validate_security(int(handle), directory=False)
            _validate_membership(
                parent_descriptor,
                int(handle),
                basename,
            )
            if created:
                win32file.FlushFileBuffers(int(handle))
        except OSError, pywintypes.error:
            _close_handle(
                handle,
                _native_error(NativeFailureKind.UNSAFE),
            )
        except BaseException as error:
            _close_handle(handle, error)
        descriptor = _descriptor_from_handle(
            handle,
            os.O_RDWR | os.O_BINARY,
        )
        try:
            _validate_stat(
                _metadata(descriptor, NativeFailureKind.UNSAFE),
                directory=False,
            )
            return os.fdopen(descriptor, "r+b", buffering=0)
        except OSError:
            _close_descriptor(descriptor)
            raise _native_error(NativeFailureKind.UNSAFE) from None
        except NativeFilesystemError as error:
            _close_descriptor(descriptor, error)

    class WindowsPlatform:
        """NTFS adapter using pywin32 file and security APIs."""

        def qualify(self, parent: Path) -> FilesystemFamily:
            """Require a fixed local NTFS volume with persistent ACLs."""
            ancestor = _existing_ancestor(parent)
            descriptor = _open_directory(ancestor, private=False)
            with _owned_descriptor(
                descriptor,
                NativeFailureKind.UNSUPPORTED,
            ):
                return _qualify_local_ntfs(ancestor)

        def ensure_parent(self, parent: Path) -> None:
            """Create or validate a protected Sidekick-owned directory."""
            _ensure_private_parent(parent)

        def repair_parent_permissions(self, parent: Path) -> bool:
            """Install the exact DACL on a current-user-owned parent."""
            attributes = _path_attributes(parent)
            if attributes is None:
                return False
            if (
                attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
            ):
                raise _native_error(NativeFailureKind.UNSAFE)
            parent_descriptor = _open_directory(
                parent.parent,
                private=False,
            )
            with _owned_descriptor(
                parent_descriptor,
                NativeFailureKind.HARDEN,
            ):
                if not _require_exact_entry(parent.parent, parent.name):
                    raise _native_error(NativeFailureKind.CHANGED)
                descriptor = _open_security_repair_directory(parent)
                with _owned_descriptor(
                    descriptor,
                    NativeFailureKind.HARDEN,
                ):
                    handle = msvcrt.get_osfhandle(descriptor)
                    _validate_membership(
                        parent_descriptor,
                        handle,
                        parent.name,
                    )
                    before = _metadata(
                        descriptor,
                        NativeFailureKind.UNSAFE,
                    )
                    _validate_repair_owner(handle)
                    security_valid = True
                    try:
                        _validate_security(handle, directory=True)
                    except NativeFilesystemError:
                        security_valid = False
                    if security_valid:
                        return False
                    _repair_security(handle, directory=True)
                    after = _metadata(
                        descriptor,
                        NativeFailureKind.HARDEN,
                    )
                    if (after.st_dev, after.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ) or not _require_exact_entry(
                        parent.parent,
                        parent.name,
                    ):
                        raise _native_error(NativeFailureKind.CHANGED)
                    _validate_membership(
                        parent_descriptor,
                        handle,
                        parent.name,
                    )
                    return True

        def list_basenames(self, parent: Path) -> tuple[str, ...]:
            """List sibling names after validating the parent handle."""
            attributes = _path_attributes(parent)
            if attributes is None:
                return ()
            if (
                attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
            ):
                raise _native_error(NativeFailureKind.UNSAFE)
            descriptor = _open_directory(parent)
            with _owned_descriptor(
                descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                try:
                    return tuple(entry.name for entry in parent.iterdir())
                except OSError:
                    raise _native_error(NativeFailureKind.UNREADABLE) from None

        def read(
            self,
            parent: Path,
            basename: str,
            limit: int,
        ) -> NativeFile | None:
            """Read one bounded protected non-reparse sibling."""
            return _read_file(parent, basename, limit)

        def create_private(
            self,
            parent: Path,
            basename: str,
            data: bytes,
        ) -> NativeFile:
            """Create a private write-through file and verify it."""
            return _create_private_file(parent, basename, data)

        def publish_no_replace(
            self,
            parent: Path,
            temporary_basename: str,
            final_basename: str,
            device: int,
            inode: int,
        ) -> None:
            """Publish by validated no-replace write-through move."""
            _publish_no_replace(
                parent,
                temporary_basename,
                final_basename,
                device,
                inode,
            )

        def replace(
            self,
            parent: Path,
            temporary_basename: str,
            final_basename: str,
            *,
            destination_exists: bool,
            device: int,
            inode: int,
        ) -> None:
            """Replace authority through the approved Windows primitives."""
            _replace_file(
                parent,
                temporary_basename,
                final_basename,
                destination_exists=destination_exists,
                device=device,
                inode=inode,
            )

        def harden(
            self,
            parent: Path,
            final_basename: str,
            limit: int,
        ) -> None:
            """Flush and security-check the final non-reparse object."""
            _harden_file(parent, final_basename, limit)

        def harden_cleanup(self, parent: Path) -> None:
            """Retain Windows' documented best-effort namespace boundary."""
            del parent

        def remove_candidate(
            self,
            parent: Path,
            basename: str,
        ) -> bool:
            """Delete one exact owned temporary when present."""
            return _remove_candidate(parent, basename)

        def remove_validated(
            self,
            parent: Path,
            basename: str,
            device: int,
            inode: int,
        ) -> bool:
            """Remove only the exact previously validated identity."""
            return _remove_validated(
                parent,
                basename,
                device,
                inode,
            )

        def open_lock(self, parent: Path, basename: str) -> IO[bytes]:
            """Open a private non-reparse sidecar for low-level locking."""
            parent_descriptor = _open_directory(parent)
            stream: IO[bytes] | None = None
            try:
                with _owned_descriptor(
                    parent_descriptor,
                    NativeFailureKind.UNSAFE,
                ):
                    stream = _open_lock_child(
                        parent,
                        basename,
                        parent_descriptor,
                    )
            except BaseException as error:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        error.add_note("Native lock cleanup also failed.")
                raise
            if stream is None:
                raise _native_error(NativeFailureKind.UNSAFE)
            return stream

        def prove_lock_identity(
            self,
            parent: Path,
            basename: str,
            sidecar: IO[bytes],
        ) -> None:
            """Reprove the locked handle's security and path membership."""
            parent_descriptor = _open_directory(parent)
            with _owned_descriptor(
                parent_descriptor,
                NativeFailureKind.UNSAFE,
            ):
                try:
                    descriptor = sidecar.fileno()
                    handle = msvcrt.get_osfhandle(descriptor)
                except OSError, ValueError:
                    raise _native_error(NativeFailureKind.UNSAFE) from None
                _validate_security(handle, directory=False)
                _validate_stat(
                    _metadata(descriptor, NativeFailureKind.UNSAFE),
                    directory=False,
                )
                _validate_membership(parent_descriptor, handle, basename)


__all__ = ["WindowsPlatform"]
