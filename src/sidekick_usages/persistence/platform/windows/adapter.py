"""NTFS persistence adapter implemented through pywin32."""

import os
import stat
import sys
from pathlib import Path
from typing import IO

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.types import (
    FilesystemFamily,
    NativeFailureKind,
)

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32con
    import win32file
    import winerror

    from sidekick_usages.persistence.platform.windows import (
        files,
        handles,
        namespace,
        security,
    )

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
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        descriptor = handles.descriptor_from_handle(
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            handles.validate_stat(
                handles.metadata(descriptor, NativeFailureKind.UNSAFE),
                directory=True,
            )
            return descriptor
        except NativeFilesystemError as error:
            handles.close_descriptor(descriptor, error)

    def _close_descriptor_stack(
        descriptors: list[int],
        primary: NativeFilesystemError | None = None,
    ) -> None:
        failure = primary
        for descriptor in reversed(descriptors):
            try:
                handles.close_descriptor(descriptor)
            except NativeFilesystemError as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Native descriptor cleanup also failed.")
        if failure is not None:
            raise failure from None

    def _open_or_create_directory(path: Path, *, private: bool) -> int:
        attributes = namespace.path_attributes(path)
        if attributes is None:
            try:
                win32file.CreateDirectoryW(
                    str(path),
                    security.private_security_attributes(directory=True),
                )
            except pywintypes.error as error:
                if error.winerror != winerror.ERROR_ALREADY_EXISTS:
                    raise NativeFilesystemError(
                        NativeFailureKind.CREATE
                    ) from None
        elif (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
        ):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        return files.open_directory(path, private=private)

    def _ensure_private_parent(parent: Path) -> None:
        ancestor = namespace.existing_ancestor(parent)
        components = parent.relative_to(ancestor).parts
        descriptors = [files.open_directory(ancestor, private=False)]
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
                security.validate_security(
                    msvcrt.get_osfhandle(descriptors[-1]),
                    directory=True,
                )
        except OSError:
            _close_descriptor_stack(
                descriptors,
                NativeFilesystemError(NativeFailureKind.UNSAFE),
            )
        except NativeFilesystemError as error:
            _close_descriptor_stack(descriptors, error)
        _close_descriptor_stack(descriptors)

    def _open_lock_child(
        parent: Path,
        basename: str,
        parent_descriptor: int,
    ) -> IO[bytes]:
        path = namespace.child_path(parent, basename)
        created = False
        try:
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                security.private_security_attributes(directory=False),
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
                raise NativeFilesystemError(NativeFailureKind.CREATE) from None
            if not namespace.require_exact_entry(parent, basename):
                raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
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
                raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        try:
            security.validate_security(int(handle), directory=False)
            namespace.validate_membership(
                parent_descriptor,
                int(handle),
                basename,
            )
            if created:
                win32file.FlushFileBuffers(int(handle))
        except OSError, pywintypes.error:
            handles.close_handle(
                handle,
                NativeFilesystemError(NativeFailureKind.UNSAFE),
            )
        except BaseException as error:
            handles.close_handle(handle, error)
        descriptor = handles.descriptor_from_handle(
            handle,
            os.O_RDWR | os.O_BINARY,
        )
        try:
            handles.validate_stat(
                handles.metadata(descriptor, NativeFailureKind.UNSAFE),
                directory=False,
            )
            return os.fdopen(descriptor, "r+b", buffering=0)
        except OSError:
            handles.close_descriptor(descriptor)
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        except NativeFilesystemError as error:
            handles.close_descriptor(descriptor, error)

    class WindowsPlatform:
        """NTFS adapter using pywin32 file and security APIs."""

        def qualify(self, parent: Path) -> FilesystemFamily:
            """Require a fixed local NTFS volume with persistent ACLs."""
            ancestor = namespace.existing_ancestor(parent)
            descriptor = files.open_directory(ancestor, private=False)
            with handles.owned_descriptor(
                descriptor,
                NativeFailureKind.UNSUPPORTED,
            ):
                return namespace.qualify_local_ntfs(ancestor)

        def ensure_parent(self, parent: Path) -> None:
            """Create or validate a protected Sidekick-owned directory."""
            _ensure_private_parent(parent)

        def repair_parent_permissions(self, parent: Path) -> bool:
            """Install the exact DACL on a current-user-owned parent."""
            attributes = namespace.path_attributes(parent)
            if attributes is None:
                return False
            if (
                attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
            ):
                raise NativeFilesystemError(NativeFailureKind.UNSAFE)
            parent_descriptor = files.open_directory(
                parent.parent,
                private=False,
            )
            with handles.owned_descriptor(
                parent_descriptor,
                NativeFailureKind.HARDEN,
            ):
                if not namespace.require_exact_entry(
                    parent.parent, parent.name
                ):
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                descriptor = _open_security_repair_directory(parent)
                with handles.owned_descriptor(
                    descriptor,
                    NativeFailureKind.HARDEN,
                ):
                    handle = msvcrt.get_osfhandle(descriptor)
                    namespace.validate_membership(
                        parent_descriptor,
                        handle,
                        parent.name,
                    )
                    before = handles.metadata(
                        descriptor,
                        NativeFailureKind.UNSAFE,
                    )
                    security.validate_repair_owner(handle)
                    security_valid = True
                    try:
                        security.validate_security(handle, directory=True)
                    except NativeFilesystemError:
                        security_valid = False
                    if security_valid:
                        return False
                    security.repair_security(handle, directory=True)
                    after = handles.metadata(
                        descriptor,
                        NativeFailureKind.HARDEN,
                    )
                    if (after.st_dev, after.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ) or not namespace.require_exact_entry(
                        parent.parent,
                        parent.name,
                    ):
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    namespace.validate_membership(
                        parent_descriptor,
                        handle,
                        parent.name,
                    )
                    return True

        def list_basenames(self, parent: Path) -> tuple[str, ...]:
            """List sibling names after validating the parent handle."""
            attributes = namespace.path_attributes(parent)
            if attributes is None:
                return ()
            if (
                attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
            ):
                raise NativeFilesystemError(NativeFailureKind.UNSAFE)
            descriptor = files.open_directory(parent)
            with handles.owned_descriptor(
                descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                try:
                    return tuple(entry.name for entry in parent.iterdir())
                except OSError:
                    raise NativeFilesystemError(
                        NativeFailureKind.UNREADABLE
                    ) from None

        def read(
            self,
            parent: Path,
            basename: str,
            limit: int,
        ) -> NativeFile | None:
            """Read one bounded protected non-reparse sibling."""
            return files.read_file(parent, basename, limit)

        def read_provider_owned(
            self,
            parent: Path,
            basename: str,
            limit: int,
        ) -> NativeFile | None:
            """Reject native provider credential reads on Windows."""
            del parent, basename, limit
            raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)

        def create_private(
            self,
            parent: Path,
            basename: str,
            data: bytes,
        ) -> NativeFile:
            """Create a private write-through file and verify it."""
            return files.create_private_file(parent, basename, data)

        def publish_no_replace(
            self,
            parent: Path,
            temporary_basename: str,
            final_basename: str,
            device: int,
            inode: int,
        ) -> None:
            """Publish by validated no-replace write-through move."""
            files.publish_no_replace(
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
            files.replace_file(
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
            files.harden_file(parent, final_basename, limit)

        def harden_cleanup(self, parent: Path) -> None:
            """Retain Windows' documented best-effort namespace boundary."""
            del parent

        def remove_candidate(
            self,
            parent: Path,
            basename: str,
        ) -> bool:
            """Delete one exact owned temporary when present."""
            return files.remove_candidate(parent, basename)

        def remove_validated(
            self,
            parent: Path,
            basename: str,
            device: int,
            inode: int,
        ) -> bool:
            """Remove only the exact previously validated identity."""
            return files.remove_validated(
                parent,
                basename,
                device,
                inode,
            )

        def open_lock(self, parent: Path, basename: str) -> IO[bytes]:
            """Open a private non-reparse sidecar for low-level locking."""
            parent_descriptor = files.open_directory(parent)
            stream: IO[bytes] | None = None
            try:
                with handles.owned_descriptor(
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
                raise NativeFilesystemError(NativeFailureKind.UNSAFE)
            return stream

        def prove_lock_identity(
            self,
            parent: Path,
            basename: str,
            sidecar: IO[bytes],
        ) -> None:
            """Reprove the locked handle's security and path membership."""
            parent_descriptor = files.open_directory(parent)
            with handles.owned_descriptor(
                parent_descriptor,
                NativeFailureKind.UNSAFE,
            ):
                try:
                    descriptor = sidecar.fileno()
                    handle = msvcrt.get_osfhandle(descriptor)
                except OSError, ValueError:
                    raise NativeFilesystemError(
                        NativeFailureKind.UNSAFE
                    ) from None
                security.validate_security(handle, directory=False)
                handles.validate_stat(
                    handles.metadata(descriptor, NativeFailureKind.UNSAFE),
                    directory=False,
                )
                namespace.validate_membership(
                    parent_descriptor, handle, basename
                )
