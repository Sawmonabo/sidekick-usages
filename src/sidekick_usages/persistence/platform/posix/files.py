"""Bounded POSIX regular-file validation and reads."""

import os
import stat

from sidekick_usages.persistence.platform.contracts import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)

_READ_CHUNK_BYTES = 64 * 1024


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
    )


__all__ = ["read_descriptor", "validate_file"]
