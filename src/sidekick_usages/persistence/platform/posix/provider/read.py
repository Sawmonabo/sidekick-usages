"""Provider credential reads through held POSIX descriptors."""

import os
import stat
from collections.abc import Callable
from pathlib import Path

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.posix import files, namespace
from sidekick_usages.persistence.platform.types import NativeFailureKind

_PROVIDER_DIRECTORY_MODES = frozenset({0o700, 0o755})


def read_provider_owned(
    parent: Path,
    basename: str,
    limit: int,
    *,
    qualify_descriptor: Callable[[int], None],
    validate_descriptor: Callable[[int], None],
) -> NativeFile | None:
    """Read one provider file through a stable qualified parent."""
    metadata = namespace.path_metadata(parent)
    if metadata is None:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    parent_descriptor = namespace.open_directory(parent, private=False)
    with namespace.owned_descriptor(
        parent_descriptor,
        NativeFailureKind.UNREADABLE,
    ):
        expected_parent = _validate_parent(
            parent_descriptor,
            metadata,
            qualify_descriptor,
            validate_descriptor,
        )
        result = files.read_held_file(
            parent_descriptor,
            basename,
            limit,
            allow_interrupted_link=False,
            descriptor_validator=validate_descriptor,
        )
        _revalidate_parent(
            parent,
            parent_descriptor,
            expected_parent,
            validate_descriptor,
        )
        return result


def _validate_parent(
    descriptor: int,
    path_metadata: os.stat_result,
    qualify_descriptor: Callable[[int], None],
    validate_descriptor: Callable[[int], None],
) -> tuple[int, ...]:
    """Qualify one held current-user provider directory."""
    try:
        held = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    if (
        _parent_state(path_metadata) != _parent_state(held)
        or held.st_uid != os.geteuid()
        or stat.S_IMODE(held.st_mode) not in _PROVIDER_DIRECTORY_MODES
    ):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    qualify_descriptor(descriptor)
    validate_descriptor(descriptor)
    return _parent_state(held)


def _revalidate_parent(
    parent: Path,
    descriptor: int,
    expected: tuple[int, ...],
    validate_descriptor: Callable[[int], None],
) -> None:
    """Require held and configured provider directories unchanged."""
    current_path = namespace.path_metadata(parent)
    try:
        held = os.fstat(descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    if (
        current_path is None
        or _parent_state(current_path) != expected
        or _parent_state(held) != expected
    ):
        raise NativeFilesystemError(NativeFailureKind.CHANGED)
    validate_descriptor(descriptor)


def _parent_state(metadata: os.stat_result) -> tuple[int, ...]:
    """Return security-relevant provider-directory metadata."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
