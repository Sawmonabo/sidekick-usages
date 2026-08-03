"""Bounded POSIX regular-file validation and reads."""

import errno
import os
import stat
from collections.abc import Callable

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import (
    NativeFile,
    ShellNativeFile,
)
from sidekick_usages.persistence.platform.posix import namespace
from sidekick_usages.persistence.platform.types import NativeFailureKind

_READ_CHUNK_BYTES = 64 * 1024


def _accept_descriptor(descriptor: int) -> None:
    """Accept a descriptor covered by the caller's parent policy."""
    del descriptor


def validate_file(
    metadata: os.stat_result,
    directory_device: int,
    *,
    allow_interrupted_link: bool,
) -> None:
    """Require an owner-private regular file on the parent filesystem."""
    allowed_links = {1, 2} if allow_interrupted_link else {1}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink not in allowed_links
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or not stat.S_IMODE(metadata.st_mode) & stat.S_IRUSR
        or metadata.st_dev != directory_device
    ):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)


def read_descriptor(
    descriptor: int,
    directory_device: int,
    limit: int,
    *,
    allow_interrupted_link: bool = True,
) -> NativeFile:
    """Bounded-read one validated descriptor and reject concurrent change."""
    try:
        before = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    validate_file(
        before,
        directory_device,
        allow_interrupted_link=allow_interrupted_link,
    )
    if before.st_size > limit:
        raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)

    chunks: list[bytes] = []
    remaining = limit + 1
    try:
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    data = b"".join(chunks)
    if len(data) > limit:
        raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)

    try:
        after = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    validate_file(
        after,
        directory_device,
        allow_interrupted_link=allow_interrupted_link,
    )
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or len(data) != after.st_size
    ):
        raise NativeFilesystemError(NativeFailureKind.CHANGED)
    return NativeFile(
        device=after.st_dev,
        inode=after.st_ino,
        link_count=after.st_nlink,
        data=data,
        modified_nanoseconds=after.st_mtime_ns,
    )


def _validate_shell_file(
    metadata: os.stat_result,
    directory_device: int,
    *,
    owner_only: bool,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    unsafe_mode = (
        mode != namespace.PRIVATE_FILE_MODE
        if owner_only
        else (bool(mode & 0o022) or not mode & stat.S_IRUSR)
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_dev != directory_device
        or unsafe_mode
    ):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)


def read_shell_descriptor(
    descriptor: int,
    directory_device: int,
    limit: int,
    *,
    owner_only: bool,
) -> ShellNativeFile:
    """Stable-read one current-user shell file under its mode policy."""
    try:
        before = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    _validate_shell_file(before, directory_device, owner_only=owner_only)
    if before.st_size > limit:
        raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)
    chunks: list[bytes] = []
    remaining = limit + 1
    try:
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    data = b"".join(chunks)
    if len(data) > limit:
        raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)
    try:
        after = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    _validate_shell_file(after, directory_device, owner_only=owner_only)
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or len(data) != after.st_size
    ):
        raise NativeFilesystemError(NativeFailureKind.CHANGED)
    return ShellNativeFile(
        device=after.st_dev,
        inode=after.st_ino,
        data=data,
        modified_nanoseconds=after.st_mtime_ns,
        mode=stat.S_IMODE(after.st_mode),
    )


def read_held_file(
    parent_descriptor: int,
    basename: str,
    limit: int,
    *,
    allow_interrupted_link: bool,
    descriptor_validator: Callable[[int], None] = _accept_descriptor,
) -> NativeFile | None:
    """Open and read one exact sibling relative to a held directory."""
    return _read_held_file(
        parent_descriptor,
        basename,
        limit,
        descriptor_validator=descriptor_validator,
        reader=lambda descriptor, device, maximum: read_descriptor(
            descriptor,
            device,
            maximum,
            allow_interrupted_link=allow_interrupted_link,
        ),
    )


def read_held_shell_file(
    parent_descriptor: int,
    basename: str,
    limit: int,
    *,
    owner_only: bool,
) -> ShellNativeFile | None:
    """Open a shell sibling relative to one held qualified directory."""
    return _read_held_file(
        parent_descriptor,
        basename,
        limit,
        descriptor_validator=_accept_descriptor,
        reader=lambda descriptor, device, maximum: read_shell_descriptor(
            descriptor,
            device,
            maximum,
            owner_only=owner_only,
        ),
    )


def _read_held_file[T](
    parent_descriptor: int,
    basename: str,
    limit: int,
    *,
    descriptor_validator: Callable[[int], None],
    reader: Callable[[int, int, int], T],
) -> T | None:
    """Read one exact sibling through a caller-selected mode policy."""
    expected_identity = namespace.require_exact_entry(
        parent_descriptor,
        basename,
    )
    if expected_identity is None:
        return None
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
            raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
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
        descriptor_validator(file_descriptor)
        result = reader(file_descriptor, directory_device, limit)
        descriptor_validator(file_descriptor)
        if (
            namespace.require_exact_entry(
                parent_descriptor,
                basename,
            )
            != expected_identity
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        return result
