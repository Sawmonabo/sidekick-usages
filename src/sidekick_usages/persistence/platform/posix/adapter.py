"""Linux and WSL descriptor-relative persistence adapter."""

import errno
import os
import stat
import sys
from pathlib import Path
from typing import IO, Never

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.posix import files, namespace
from sidekick_usages.persistence.platform.types import (
    FilesystemFamily,
    NativeFailureKind,
)

if sys.platform.startswith("linux"):
    from sidekick_usages.persistence.platform.posix import mounts
else:
    mounts = None


def _open_lock_descriptor(
    parent_descriptor: int,
    basename: str,
) -> tuple[int, bool]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NONBLOCK
        | namespace.no_follow_flag()
    )
    created = False
    expected_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            basename,
            flags,
            namespace.PRIVATE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        created = True
    except FileExistsError:
        expected_identity = namespace.require_exact_entry(
            parent_descriptor,
            basename,
        )
        if expected_identity is None:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        try:
            descriptor = os.open(
                basename,
                os.O_RDWR
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | namespace.no_follow_flag(),
                dir_fd=parent_descriptor,
            )
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.CREATE) from None
    try:
        if created:
            os.fchmod(descriptor, namespace.PRIVATE_FILE_MODE)
        metadata = os.fstat(descriptor)
        parent_metadata = os.fstat(parent_descriptor)
        files.validate_file(
            metadata,
            parent_metadata.st_dev,
            allow_interrupted_link=False,
        )
        if not created and (
            expected_identity != (metadata.st_dev, metadata.st_ino)
            or namespace.require_exact_entry(parent_descriptor, basename)
            != expected_identity
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
    except OSError:
        namespace.close_descriptor(
            descriptor,
            NativeFilesystemError(NativeFailureKind.UNSAFE),
        )
    except NativeFilesystemError as error:
        namespace.close_descriptor(descriptor, error)
    except BaseException as error:
        namespace.close_descriptor(descriptor, error)
    return descriptor, created


def _remove_exact_entry(
    parent_descriptor: int,
    basename: str,
    expected_identity: tuple[int, int],
    *,
    allow_interrupted_link: bool,
) -> None:
    """Unlink a held regular file and prove its link-count transition."""
    flags = (
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | namespace.no_follow_flag()
    )
    try:
        file_descriptor = os.open(
            basename,
            flags,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        raise NativeFilesystemError(NativeFailureKind.CHANGED) from None
    except OSError as error:
        kind = (
            NativeFailureKind.UNSAFE
            if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}
            else NativeFailureKind.REMOVE
        )
        raise NativeFilesystemError(kind) from None

    with namespace.owned_descriptor(file_descriptor, NativeFailureKind.REMOVE):
        try:
            before = os.fstat(file_descriptor)
            directory_device = os.fstat(parent_descriptor).st_dev
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.REMOVE) from None
        files.validate_file(
            before,
            directory_device,
            allow_interrupted_link=allow_interrupted_link,
        )
        if (before.st_dev, before.st_ino) != expected_identity:
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        if (
            namespace.require_exact_entry(parent_descriptor, basename)
            != expected_identity
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)

        try:
            os.unlink(basename, dir_fd=parent_descriptor)
        except FileNotFoundError:
            raise NativeFilesystemError(NativeFailureKind.CHANGED) from None
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.REMOVE) from None

        try:
            after = os.fstat(file_descriptor)
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.REMOVE) from None
        if (
            (after.st_dev, after.st_ino) != expected_identity
            or after.st_nlink != before.st_nlink - 1
            or namespace.require_exact_entry(parent_descriptor, basename)
            is not None
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)


def _fail_lock_open(
    parent_descriptor: int,
    file_descriptor: int | None,
    error: BaseException,
) -> Never:
    if file_descriptor is not None:
        try:
            namespace.close_descriptor(file_descriptor)
        except NativeFilesystemError:
            error.add_note("Native descriptor cleanup also failed.")
    namespace.close_descriptor(parent_descriptor, error)
    raise error from None


class PosixPlatform:
    """Linux/WSL adapter using one securely opened parent directory."""

    def qualify(self, parent: Path) -> FilesystemFamily:
        """Require an allowlisted mount containing the actual directory."""
        if mounts is None:
            raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
        ancestor = namespace.existing_ancestor(parent)
        descriptor = namespace.open_directory(ancestor, private=False)
        with namespace.owned_descriptor(
            descriptor,
            NativeFailureKind.UNSUPPORTED,
        ):
            return mounts.filesystem_for_descriptor(descriptor)

    def ensure_parent(self, parent: Path) -> None:
        """Create only the Sidekick-owned leaf with owner-only access."""
        ancestor_path = namespace.existing_ancestor(parent)
        descriptors = [namespace.open_directory(ancestor_path, private=False)]
        components = parent.relative_to(ancestor_path).parts
        try:
            namespace.extend_parent_chain(descriptors, components)
            if not components:
                metadata = os.fstat(descriptors[-1])
                if (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        except OSError:
            namespace.close_descriptor_stack(
                descriptors,
                NativeFilesystemError(NativeFailureKind.UNSAFE),
            )
        except NativeFilesystemError as error:
            namespace.close_descriptor_stack(descriptors, error)
        except BaseException as error:
            namespace.close_descriptor_stack(descriptors, error)
        namespace.close_descriptor_stack(descriptors)

    def repair_parent_permissions(self, parent: Path) -> bool:
        """Harden one owner-owned non-writable released parent to 0700."""
        metadata = namespace.path_metadata(parent)
        if metadata is None:
            return False
        parent_descriptor = namespace.open_directory(
            parent.parent, private=False
        )
        with namespace.owned_descriptor(
            parent_descriptor,
            NativeFailureKind.HARDEN,
        ):
            expected = namespace.require_exact_entry(
                parent_descriptor, parent.name
            )
            if expected is None:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            descriptor = namespace.open_child_directory(
                parent_descriptor,
                parent.name,
                private=False,
            )
            with namespace.owned_descriptor(
                descriptor, NativeFailureKind.HARDEN
            ):
                before = os.fstat(descriptor)
                mode = stat.S_IMODE(before.st_mode)
                if (
                    (before.st_dev, before.st_ino) != expected
                    or before.st_uid != os.geteuid()
                    or mode & 0o022
                    or namespace.require_exact_entry(
                        parent_descriptor, parent.name
                    )
                    != expected
                ):
                    raise NativeFilesystemError(NativeFailureKind.UNSAFE)
                if mode == namespace.PRIVATE_DIRECTORY_MODE:
                    return False
                try:
                    os.fchmod(descriptor, namespace.PRIVATE_DIRECTORY_MODE)
                    os.fsync(descriptor)
                    os.fsync(parent_descriptor)
                    after = os.fstat(descriptor)
                except OSError:
                    raise NativeFilesystemError(
                        NativeFailureKind.SYNCHRONIZE
                    ) from None
                if (
                    (after.st_dev, after.st_ino) != expected
                    or after.st_uid != os.geteuid()
                    or stat.S_IMODE(after.st_mode)
                    != namespace.PRIVATE_DIRECTORY_MODE
                    or namespace.require_exact_entry(
                        parent_descriptor, parent.name
                    )
                    != expected
                ):
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                return True

    def list_basenames(self, parent: Path) -> tuple[str, ...]:
        """List names through the protected parent descriptor."""
        metadata = namespace.path_metadata(parent)
        if metadata is None:
            return ()
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(
            descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            try:
                return tuple(os.listdir(descriptor))
            except OSError as error:
                kind = (
                    NativeFailureKind.UNSAFE
                    if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}
                    else NativeFailureKind.UNREADABLE
                )
                raise NativeFilesystemError(kind) from None

    def read(
        self,
        parent: Path,
        basename: str,
        limit: int,
    ) -> NativeFile | None:
        """Bounded-read a no-follow protected sibling."""
        return self._read_file(
            parent,
            basename,
            limit,
            private_parent=True,
            allow_interrupted_link=True,
        )

    def _read_file(
        self,
        parent: Path,
        basename: str,
        limit: int,
        *,
        private_parent: bool,
        allow_interrupted_link: bool,
    ) -> NativeFile | None:
        metadata = namespace.path_metadata(parent)
        if metadata is None:
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        parent_descriptor = namespace.open_directory(
            parent, private=private_parent
        )
        with namespace.owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            expected_identity = namespace.require_exact_entry(
                parent_descriptor,
                basename,
            )
            if expected_identity is None:
                return None
            flags = (
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | namespace.no_follow_flag()
            )
            try:
                file_descriptor = os.open(
                    basename,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                raise NativeFilesystemError(
                    NativeFailureKind.CHANGED
                ) from None
            except OSError as error:
                kind = (
                    NativeFailureKind.UNSAFE
                    if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}
                    else NativeFailureKind.UNREADABLE
                )
                raise NativeFilesystemError(kind) from None
            with namespace.owned_descriptor(
                file_descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                try:
                    file_metadata = os.fstat(file_descriptor)
                    directory_device = os.fstat(parent_descriptor).st_dev
                except OSError:
                    raise NativeFilesystemError(
                        NativeFailureKind.UNREADABLE
                    ) from None
                if (
                    file_metadata.st_dev,
                    file_metadata.st_ino,
                ) != expected_identity:
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                if (
                    namespace.require_exact_entry(
                        parent_descriptor,
                        basename,
                    )
                    != expected_identity
                ):
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                result = files.read_descriptor(
                    file_descriptor,
                    directory_device,
                    limit,
                    allow_interrupted_link=allow_interrupted_link,
                )
                if (
                    namespace.require_exact_entry(
                        parent_descriptor,
                        basename,
                    )
                    != expected_identity
                ):
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                return result

    def _synchronize_file(self, descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError:
            raise NativeFilesystemError(
                NativeFailureKind.SYNCHRONIZE
            ) from None

    def _synchronize_created_lock(
        self,
        file_descriptor: int,
        parent_descriptor: int,
    ) -> None:
        try:
            self._synchronize_file(file_descriptor)
            os.fsync(parent_descriptor)
        except NativeFilesystemError:
            raise
        except OSError:
            raise NativeFilesystemError(
                NativeFailureKind.SYNCHRONIZE
            ) from None

    def create_private(
        self,
        parent: Path,
        basename: str,
        data: bytes,
    ) -> NativeFile:
        """Create and verify a synchronized owner-only sibling."""
        parent_descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(
            parent_descriptor,
            NativeFailureKind.CREATE,
        ):
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | namespace.no_follow_flag()
            )
            try:
                file_descriptor = os.open(
                    basename,
                    flags,
                    namespace.PRIVATE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                raise NativeFilesystemError(NativeFailureKind.EXISTS) from None
            except OSError:
                raise NativeFilesystemError(NativeFailureKind.CREATE) from None

            with namespace.owned_descriptor(
                file_descriptor,
                NativeFailureKind.WRITE,
            ):
                try:
                    os.fchmod(file_descriptor, namespace.PRIVATE_FILE_MODE)
                except OSError:
                    raise NativeFilesystemError(
                        NativeFailureKind.CREATE
                    ) from None
                view = memoryview(data)
                written = 0
                try:
                    while written < len(view):
                        count = os.write(file_descriptor, view[written:])
                        if count <= 0:
                            raise NativeFilesystemError(
                                NativeFailureKind.WRITE
                            )
                        written += count
                except OSError:
                    raise NativeFilesystemError(
                        NativeFailureKind.WRITE
                    ) from None
                self._synchronize_file(file_descriptor)

        reopened = self.read(parent, basename, len(data))
        if reopened is None or reopened.data != data:
            raise NativeFilesystemError(NativeFailureKind.WRITE)
        return reopened

    def publish_no_replace(
        self,
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        device: int,
        inode: int,
    ) -> None:
        """Publish through descriptor-relative atomic ``link``."""
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(descriptor, NativeFailureKind.PUBLISH):
            if namespace.require_exact_entry(
                descriptor,
                temporary_basename,
            ) != (device, inode):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            try:
                os.link(
                    temporary_basename,
                    final_basename,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise NativeFilesystemError(NativeFailureKind.EXISTS) from None
            except OSError:
                raise NativeFilesystemError(
                    NativeFailureKind.PUBLISH
                ) from None

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
        """Replace existing state or publish a first write no-clobber."""
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(descriptor, NativeFailureKind.REPLACE):
            if namespace.require_exact_entry(
                descriptor,
                temporary_basename,
            ) != (device, inode):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            try:
                if destination_exists:
                    os.replace(
                        temporary_basename,
                        final_basename,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                else:
                    os.link(
                        temporary_basename,
                        final_basename,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary_basename, dir_fd=descriptor)
            except FileExistsError:
                raise NativeFilesystemError(NativeFailureKind.EXISTS) from None
            except OSError:
                raise NativeFilesystemError(
                    NativeFailureKind.REPLACE
                ) from None

    def harden(
        self,
        parent: Path,
        final_basename: str,
        limit: int,
    ) -> None:
        """Synchronize the parent namespace after a native change."""
        del final_basename, limit
        self.harden_cleanup(parent)

    def harden_cleanup(self, parent: Path) -> None:
        """Synchronize the parent namespace after temporary cleanup."""
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(descriptor, NativeFailureKind.HARDEN):
            try:
                os.fsync(descriptor)
            except OSError:
                raise NativeFilesystemError(NativeFailureKind.HARDEN) from None

    def remove_candidate(
        self,
        parent: Path,
        basename: str,
    ) -> bool:
        """Remove only an exact single-link temporary name."""
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            expected_identity = namespace.require_exact_entry(
                descriptor, basename
            )
            if expected_identity is None:
                return False
            _remove_exact_entry(
                descriptor,
                basename,
                expected_identity,
                allow_interrupted_link=False,
            )
            return True

    def remove_validated(
        self,
        parent: Path,
        basename: str,
        device: int,
        inode: int,
    ) -> bool:
        """Remove only the exact previously validated identity."""
        descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            identity = namespace.require_exact_entry(descriptor, basename)
            if identity is None:
                return False
            if identity != (device, inode):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            _remove_exact_entry(
                descriptor,
                basename,
                identity,
                allow_interrupted_link=True,
            )
            return True

    def open_lock(self, parent: Path, basename: str) -> IO[bytes]:
        """Create or open and validate the persistent lock sidecar."""
        parent_descriptor = namespace.open_directory(parent, private=True)
        file_descriptor: int | None = None
        try:
            file_descriptor, created = _open_lock_descriptor(
                parent_descriptor,
                basename,
            )
            if created:
                self._synchronize_created_lock(
                    file_descriptor,
                    parent_descriptor,
                )
        except BaseException as error:
            _fail_lock_open(parent_descriptor, file_descriptor, error)
        try:
            namespace.close_descriptor(parent_descriptor)
        except NativeFilesystemError as error:
            if file_descriptor is not None:
                namespace.close_descriptor(file_descriptor, error)
            raise
        if file_descriptor is None:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        try:
            return os.fdopen(file_descriptor, "r+b", buffering=0)
        except OSError:
            namespace.close_descriptor(file_descriptor)
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None

    def prove_lock_identity(
        self,
        parent: Path,
        basename: str,
        sidecar: IO[bytes],
    ) -> None:
        """Prove the locked descriptor remains the exact named sidecar."""
        parent_descriptor = namespace.open_directory(parent, private=True)
        with namespace.owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNSAFE,
        ):
            try:
                file_descriptor = sidecar.fileno()
                locked = os.fstat(file_descriptor)
                parent_metadata = os.fstat(parent_descriptor)
            except OSError, ValueError:
                raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
            files.validate_file(
                locked,
                parent_metadata.st_dev,
                allow_interrupted_link=False,
            )
            named_identity = namespace.require_exact_entry(
                parent_descriptor,
                basename,
            )
            try:
                current = os.fstat(file_descriptor)
            except OSError:
                raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
            files.validate_file(
                current,
                parent_metadata.st_dev,
                allow_interrupted_link=False,
            )
            locked_identity = (locked.st_dev, locked.st_ino)
            if (
                named_identity is None
                or named_identity != locked_identity
                or (current.st_dev, current.st_ino) != locked_identity
            ):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)

    @staticmethod
    def _metadata(descriptor: int) -> os.stat_result:
        try:
            return os.fstat(descriptor)
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
