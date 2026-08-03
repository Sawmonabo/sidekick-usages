"""Descriptor-relative POSIX shell file operations."""

import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import ShellNativeFile
from sidekick_usages.persistence.platform.posix import files, namespace
from sidekick_usages.persistence.platform.types import NativeFailureKind

_TEMPORARY_ATTEMPTS = 16
_REVALIDATION_MARGIN_BYTES = 1024 * 1024


@contextmanager
def open_parent_descriptor(
    root: Path,
    parent: Path,
    *,
    create: bool,
) -> Iterator[int | None]:
    """Hold a shell parent reached without following any path component."""
    try:
        components = parent.relative_to(root).parts
    except ValueError:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
    descriptors = [namespace.open_absolute_directory(root, private=False)]
    primary: BaseException | None = None
    missing = False
    try:
        if create:
            namespace.extend_parent_chain(
                descriptors,
                components,
                leaf_private=False,
            )
        else:
            for component in components:
                if (
                    namespace.require_exact_entry(
                        descriptors[-1],
                        component,
                    )
                    is None
                ):
                    missing = True
                    break
                descriptors.append(
                    namespace.open_child_directory(
                        descriptors[-1],
                        component,
                        private=False,
                    )
                )
        yield None if missing else descriptors[-1]
    except BaseException as error:
        primary = error
    namespace.close_descriptor_stack(descriptors, primary)


def read_owned(
    root: Path,
    parent: Path,
    basename: str,
    limit: int,
    *,
    owner_only: bool,
) -> ShellNativeFile | None:
    """Stable-read one shell file through a held public parent."""
    with open_parent_descriptor(root, parent, create=False) as descriptor:
        if descriptor is None:
            return None
        return files.read_held_shell_file(
            descriptor,
            basename,
            limit,
            owner_only=owner_only,
        )


def write_atomic(
    root: Path,
    parent: Path,
    basename: str,
    data: bytes,
    expected: ShellNativeFile | None,
    *,
    mode: int,
    synchronize_file: Callable[[int], None],
) -> ShellNativeFile:
    """Atomically publish shell bytes after a stable expected read."""
    with open_parent_descriptor(root, parent, create=True) as descriptor:
        if descriptor is None:
            raise NativeFilesystemError(NativeFailureKind.CREATE)
        owner_only = expected is None or mode == namespace.PRIVATE_FILE_MODE
        current = files.read_held_shell_file(
            descriptor,
            basename,
            len(data) + _REVALIDATION_MARGIN_BYTES,
            owner_only=owner_only,
        )
        if current != expected:
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        temporary_basename, temporary_identity = _create_temporary(
            descriptor,
            basename,
            data,
            mode,
            synchronize_file,
        )
        try:
            current = files.read_held_shell_file(
                descriptor,
                basename,
                len(data) + _REVALIDATION_MARGIN_BYTES,
                owner_only=owner_only,
            )
            if current != expected:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            _publish_temporary(
                descriptor,
                temporary_basename,
                basename,
                temporary_identity,
                destination_exists=expected is not None,
            )
            os.fsync(descriptor)
        except BaseException:
            _remove_temporary(
                descriptor,
                temporary_basename,
                temporary_identity,
            )
            raise
        published = files.read_held_shell_file(
            descriptor,
            basename,
            len(data),
            owner_only=mode == namespace.PRIVATE_FILE_MODE,
        )
        if published is None or published.data != data:
            raise NativeFilesystemError(NativeFailureKind.WRITE)
        return published


def _create_temporary(
    parent_descriptor: int,
    basename: str,
    data: bytes,
    mode: int,
    synchronize_file: Callable[[int], None],
) -> tuple[str, tuple[int, int]]:
    if mode & 0o022 or not mode & stat.S_IRUSR:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    descriptor, temporary_basename = _open_temporary(
        parent_descriptor,
        basename,
        mode,
    )
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise NativeFilesystemError(NativeFailureKind.WRITE) from None
    identity = metadata.st_dev, metadata.st_ino
    try:
        with namespace.owned_descriptor(descriptor, NativeFailureKind.WRITE):
            metadata = _write_temporary(
                descriptor,
                parent_descriptor,
                data,
                mode,
                synchronize_file,
            )
            identity = metadata.st_dev, metadata.st_ino
            if (
                namespace.require_exact_entry(
                    parent_descriptor,
                    temporary_basename,
                )
                != identity
            ):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            return temporary_basename, identity
    except BaseException:
        _remove_temporary(
            parent_descriptor,
            temporary_basename,
            identity,
        )
        raise


def _open_temporary(
    parent_descriptor: int,
    basename: str,
    mode: int,
) -> tuple[int, str]:
    for _attempt in range(_TEMPORARY_ATTEMPTS):
        temporary_basename = f".{basename}.sidekick-{secrets.token_hex(8)}"
        try:
            return os.open(
                temporary_basename,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | namespace.no_follow_flag(),
                mode,
                dir_fd=parent_descriptor,
            ), temporary_basename
        except FileExistsError:
            continue
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.CREATE) from None
    raise NativeFilesystemError(NativeFailureKind.CREATE)


def _write_temporary(
    descriptor: int,
    parent_descriptor: int,
    data: bytes,
    mode: int,
    synchronize_file: Callable[[int], None],
) -> os.stat_result:
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError
            written += count
        synchronize_file(descriptor)
        metadata = os.fstat(descriptor)
        parent_device = os.fstat(parent_descriptor).st_dev
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.WRITE) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_dev != parent_device
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size != len(data)
    ):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    return metadata


def _publish_temporary(
    parent_descriptor: int,
    temporary_basename: str,
    basename: str,
    temporary_identity: tuple[int, int],
    *,
    destination_exists: bool,
) -> None:
    if (
        namespace.require_exact_entry(
            parent_descriptor,
            temporary_basename,
        )
        != temporary_identity
    ):
        raise NativeFilesystemError(NativeFailureKind.CHANGED)
    try:
        if destination_exists:
            os.replace(
                temporary_basename,
                basename,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:
            os.link(
                temporary_basename,
                basename,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary_basename, dir_fd=parent_descriptor)
    except FileExistsError:
        raise NativeFilesystemError(NativeFailureKind.CHANGED) from None
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.REPLACE) from None


def _remove_temporary(
    parent_descriptor: int,
    basename: str,
    expected_identity: tuple[int, int],
) -> None:
    if (
        namespace.require_exact_entry(
            parent_descriptor,
            basename,
        )
        != expected_identity
    ):
        return
    try:
        os.unlink(basename, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.REMOVE) from None
