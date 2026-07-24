"""Descriptor-relative Linux and WSL persistence operations."""

import errno
import os
import stat
from pathlib import Path
from typing import IO, Never

from sidekick_usages.persistence._platform import (
    FilesystemFamily,
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)
from sidekick_usages.persistence._platform.posix_files import (
    read_descriptor as _read_descriptor,
)
from sidekick_usages.persistence._platform.posix_files import (
    validate_file as _validate_file,
)
from sidekick_usages.persistence._platform.posix_mounts import (
    filesystem_for_descriptor,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    PRIVATE_DIRECTORY_MODE as _PRIVATE_DIRECTORY_MODE,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    PRIVATE_FILE_MODE as _PRIVATE_FILE_MODE,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    close_descriptor as _close_descriptor,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    close_descriptor_stack as _close_descriptor_stack,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    existing_ancestor as _existing_ancestor,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    extend_parent_chain as _extend_parent_chain,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    no_follow_flag as _no_follow_flag,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    open_child_directory as _open_child_directory,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    open_directory as _open_directory,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    owned_descriptor as _owned_descriptor,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    path_metadata as _path_metadata,
)
from sidekick_usages.persistence._platform.posix_namespace import (
    require_exact_entry as _require_exact_entry,
)


def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
    return NativeFilesystemError(kind)


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
        | _no_follow_flag()
    )
    created = False
    expected_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            basename,
            flags,
            _PRIVATE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        created = True
    except FileExistsError:
        expected_identity = _require_exact_entry(
            parent_descriptor,
            basename,
        )
        if expected_identity is None:
            raise _native_error(NativeFailureKind.UNSAFE) from None
        try:
            descriptor = os.open(
                basename,
                os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK | _no_follow_flag(),
                dir_fd=parent_descriptor,
            )
        except OSError:
            raise _native_error(NativeFailureKind.UNSAFE) from None
    except OSError:
        raise _native_error(NativeFailureKind.CREATE) from None
    try:
        if created:
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        metadata = os.fstat(descriptor)
        parent_metadata = os.fstat(parent_descriptor)
        _validate_file(
            metadata,
            parent_metadata.st_dev,
            allow_interrupted_link=False,
        )
        if not created and (
            expected_identity != (metadata.st_dev, metadata.st_ino)
            or _require_exact_entry(parent_descriptor, basename)
            != expected_identity
        ):
            raise _native_error(NativeFailureKind.CHANGED)
    except OSError:
        _close_descriptor(
            descriptor,
            _native_error(NativeFailureKind.UNSAFE),
        )
    except NativeFilesystemError as error:
        _close_descriptor(descriptor, error)
    except BaseException as error:
        _close_descriptor(descriptor, error)
    return descriptor, created


def _remove_exact_entry(
    parent_descriptor: int,
    basename: str,
    expected_identity: tuple[int, int],
    *,
    allow_interrupted_link: bool,
) -> None:
    """Unlink a held regular file and prove its link-count transition."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | _no_follow_flag()
    try:
        file_descriptor = os.open(
            basename,
            flags,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        raise _native_error(NativeFailureKind.CHANGED) from None
    except OSError as error:
        kind = (
            NativeFailureKind.UNSAFE
            if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}
            else NativeFailureKind.REMOVE
        )
        raise _native_error(kind) from None

    with _owned_descriptor(file_descriptor, NativeFailureKind.REMOVE):
        try:
            before = os.fstat(file_descriptor)
            directory_device = os.fstat(parent_descriptor).st_dev
        except OSError:
            raise _native_error(NativeFailureKind.REMOVE) from None
        _validate_file(
            before,
            directory_device,
            allow_interrupted_link=allow_interrupted_link,
        )
        if (before.st_dev, before.st_ino) != expected_identity:
            raise _native_error(NativeFailureKind.CHANGED)
        if (
            _require_exact_entry(parent_descriptor, basename)
            != expected_identity
        ):
            raise _native_error(NativeFailureKind.CHANGED)

        try:
            os.unlink(basename, dir_fd=parent_descriptor)
        except FileNotFoundError:
            raise _native_error(NativeFailureKind.CHANGED) from None
        except OSError:
            raise _native_error(NativeFailureKind.REMOVE) from None

        try:
            after = os.fstat(file_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.REMOVE) from None
        if (
            (after.st_dev, after.st_ino) != expected_identity
            or after.st_nlink != before.st_nlink - 1
            or _require_exact_entry(parent_descriptor, basename) is not None
        ):
            raise _native_error(NativeFailureKind.CHANGED)


def _fail_lock_open(
    parent_descriptor: int,
    file_descriptor: int | None,
    error: BaseException,
) -> Never:
    if file_descriptor is not None:
        try:
            _close_descriptor(file_descriptor)
        except NativeFilesystemError:
            error.add_note("Native descriptor cleanup also failed.")
    _close_descriptor(parent_descriptor, error)
    raise error from None


class PosixPlatform:
    """Linux/WSL adapter using one securely opened parent directory."""

    def qualify(self, parent: Path) -> FilesystemFamily:
        """Require an allowlisted mount containing the actual directory."""
        ancestor = _existing_ancestor(parent)
        descriptor = _open_directory(ancestor, private=False)
        with _owned_descriptor(
            descriptor,
            NativeFailureKind.UNSUPPORTED,
        ):
            return filesystem_for_descriptor(descriptor)

    def ensure_parent(self, parent: Path) -> None:
        """Create only the Sidekick-owned leaf with owner-only access."""
        ancestor_path = _existing_ancestor(parent)
        descriptors = [_open_directory(ancestor_path, private=False)]
        components = parent.relative_to(ancestor_path).parts
        try:
            _extend_parent_chain(descriptors, components)
            if not components:
                metadata = os.fstat(descriptors[-1])
                if (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise _native_error(NativeFailureKind.UNSAFE)
        except OSError:
            _close_descriptor_stack(
                descriptors,
                _native_error(NativeFailureKind.UNSAFE),
            )
        except NativeFilesystemError as error:
            _close_descriptor_stack(descriptors, error)
        except BaseException as error:
            _close_descriptor_stack(descriptors, error)
        _close_descriptor_stack(descriptors)

    def repair_parent_permissions(self, parent: Path) -> bool:
        """Harden one owner-owned non-writable released parent to 0700."""
        metadata = _path_metadata(parent)
        if metadata is None:
            return False
        parent_descriptor = _open_directory(parent.parent, private=False)
        with _owned_descriptor(
            parent_descriptor,
            NativeFailureKind.HARDEN,
        ):
            expected = _require_exact_entry(parent_descriptor, parent.name)
            if expected is None:
                raise _native_error(NativeFailureKind.CHANGED)
            descriptor = _open_child_directory(
                parent_descriptor,
                parent.name,
                private=False,
            )
            with _owned_descriptor(descriptor, NativeFailureKind.HARDEN):
                before = os.fstat(descriptor)
                mode = stat.S_IMODE(before.st_mode)
                if (
                    (before.st_dev, before.st_ino) != expected
                    or before.st_uid != os.geteuid()
                    or mode & 0o022
                    or _require_exact_entry(parent_descriptor, parent.name)
                    != expected
                ):
                    raise _native_error(NativeFailureKind.UNSAFE)
                if mode == _PRIVATE_DIRECTORY_MODE:
                    return False
                try:
                    os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
                    os.fsync(descriptor)
                    os.fsync(parent_descriptor)
                    after = os.fstat(descriptor)
                except OSError:
                    raise _native_error(
                        NativeFailureKind.SYNCHRONIZE
                    ) from None
                if (
                    (after.st_dev, after.st_ino) != expected
                    or after.st_uid != os.geteuid()
                    or stat.S_IMODE(after.st_mode) != _PRIVATE_DIRECTORY_MODE
                    or _require_exact_entry(parent_descriptor, parent.name)
                    != expected
                ):
                    raise _native_error(NativeFailureKind.CHANGED)
                return True

    def list_basenames(self, parent: Path) -> tuple[str, ...]:
        """List names through the protected parent descriptor."""
        metadata = _path_metadata(parent)
        if metadata is None:
            return ()
        if not stat.S_ISDIR(metadata.st_mode):
            raise _native_error(NativeFailureKind.UNSAFE)
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(
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
                raise _native_error(kind) from None

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
        metadata = _path_metadata(parent)
        if metadata is None:
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            raise _native_error(NativeFailureKind.UNSAFE)
        parent_descriptor = _open_directory(parent, private=private_parent)
        with _owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            expected_identity = _require_exact_entry(
                parent_descriptor,
                basename,
            )
            if expected_identity is None:
                return None
            flags = (
                os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | _no_follow_flag()
            )
            try:
                file_descriptor = os.open(
                    basename,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                raise _native_error(NativeFailureKind.CHANGED) from None
            except OSError as error:
                kind = (
                    NativeFailureKind.UNSAFE
                    if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}
                    else NativeFailureKind.UNREADABLE
                )
                raise _native_error(kind) from None
            with _owned_descriptor(
                file_descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                try:
                    file_metadata = os.fstat(file_descriptor)
                    directory_device = os.fstat(parent_descriptor).st_dev
                except OSError:
                    raise _native_error(NativeFailureKind.UNREADABLE) from None
                if (
                    file_metadata.st_dev,
                    file_metadata.st_ino,
                ) != expected_identity:
                    raise _native_error(NativeFailureKind.CHANGED)
                if (
                    _require_exact_entry(
                        parent_descriptor,
                        basename,
                    )
                    != expected_identity
                ):
                    raise _native_error(NativeFailureKind.CHANGED)
                result = _read_descriptor(
                    file_descriptor,
                    directory_device,
                    limit,
                    allow_interrupted_link=allow_interrupted_link,
                )
                if (
                    _require_exact_entry(
                        parent_descriptor,
                        basename,
                    )
                    != expected_identity
                ):
                    raise _native_error(NativeFailureKind.CHANGED)
                return result

    def _synchronize_file(self, descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.SYNCHRONIZE) from None

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
            raise _native_error(NativeFailureKind.SYNCHRONIZE) from None

    def create_private(
        self,
        parent: Path,
        basename: str,
        data: bytes,
    ) -> NativeFile:
        """Create and verify a synchronized owner-only sibling."""
        parent_descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(
            parent_descriptor,
            NativeFailureKind.CREATE,
        ):
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | _no_follow_flag()
            )
            try:
                file_descriptor = os.open(
                    basename,
                    flags,
                    _PRIVATE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                raise _native_error(NativeFailureKind.EXISTS) from None
            except OSError:
                raise _native_error(NativeFailureKind.CREATE) from None

            with _owned_descriptor(
                file_descriptor,
                NativeFailureKind.WRITE,
            ):
                try:
                    os.fchmod(file_descriptor, _PRIVATE_FILE_MODE)
                except OSError:
                    raise _native_error(NativeFailureKind.CREATE) from None
                view = memoryview(data)
                written = 0
                try:
                    while written < len(view):
                        count = os.write(file_descriptor, view[written:])
                        if count <= 0:
                            raise _native_error(NativeFailureKind.WRITE)
                        written += count
                except OSError:
                    raise _native_error(NativeFailureKind.WRITE) from None
                self._synchronize_file(file_descriptor)

        reopened = self.read(parent, basename, len(data))
        if reopened is None or reopened.data != data:
            raise _native_error(NativeFailureKind.WRITE)
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
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(descriptor, NativeFailureKind.PUBLISH):
            if _require_exact_entry(
                descriptor,
                temporary_basename,
            ) != (device, inode):
                raise _native_error(NativeFailureKind.CHANGED)
            try:
                os.link(
                    temporary_basename,
                    final_basename,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise _native_error(NativeFailureKind.EXISTS) from None
            except OSError:
                raise _native_error(NativeFailureKind.PUBLISH) from None

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
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(descriptor, NativeFailureKind.REPLACE):
            if _require_exact_entry(
                descriptor,
                temporary_basename,
            ) != (device, inode):
                raise _native_error(NativeFailureKind.CHANGED)
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
                raise _native_error(NativeFailureKind.EXISTS) from None
            except OSError:
                raise _native_error(NativeFailureKind.REPLACE) from None

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
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(descriptor, NativeFailureKind.HARDEN):
            try:
                os.fsync(descriptor)
            except OSError:
                raise _native_error(NativeFailureKind.HARDEN) from None

    def remove_candidate(
        self,
        parent: Path,
        basename: str,
    ) -> bool:
        """Remove only an exact single-link temporary name."""
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            expected_identity = _require_exact_entry(descriptor, basename)
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
        descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            identity = _require_exact_entry(descriptor, basename)
            if identity is None:
                return False
            if identity != (device, inode):
                raise _native_error(NativeFailureKind.CHANGED)
            _remove_exact_entry(
                descriptor,
                basename,
                identity,
                allow_interrupted_link=True,
            )
            return True

    def open_lock(self, parent: Path, basename: str) -> IO[bytes]:
        """Create or open and validate the persistent lock sidecar."""
        parent_descriptor = _open_directory(parent, private=True)
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
            _close_descriptor(parent_descriptor)
        except NativeFilesystemError as error:
            if file_descriptor is not None:
                _close_descriptor(file_descriptor, error)
            raise
        if file_descriptor is None:
            raise _native_error(NativeFailureKind.UNSAFE)
        try:
            return os.fdopen(file_descriptor, "r+b", buffering=0)
        except OSError:
            _close_descriptor(file_descriptor)
            raise _native_error(NativeFailureKind.UNSAFE) from None

    def prove_lock_identity(
        self,
        parent: Path,
        basename: str,
        sidecar: IO[bytes],
    ) -> None:
        """Prove the locked descriptor remains the exact named sidecar."""
        parent_descriptor = _open_directory(parent, private=True)
        with _owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNSAFE,
        ):
            try:
                file_descriptor = sidecar.fileno()
                locked = os.fstat(file_descriptor)
                parent_metadata = os.fstat(parent_descriptor)
            except OSError, ValueError:
                raise _native_error(NativeFailureKind.UNSAFE) from None
            _validate_file(
                locked,
                parent_metadata.st_dev,
                allow_interrupted_link=False,
            )
            named_identity = _require_exact_entry(
                parent_descriptor,
                basename,
            )
            try:
                current = os.fstat(file_descriptor)
            except OSError:
                raise _native_error(NativeFailureKind.UNSAFE) from None
            _validate_file(
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
                raise _native_error(NativeFailureKind.CHANGED)

    @staticmethod
    def _metadata(descriptor: int) -> os.stat_result:
        try:
            return os.fstat(descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNSAFE) from None


__all__ = ["PosixPlatform"]
