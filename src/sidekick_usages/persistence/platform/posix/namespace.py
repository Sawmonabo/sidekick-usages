"""Descriptor-qualified POSIX namespace operations."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.types import NativeFailureKind

_MAX_PROVIDER_DIRECTORY_ENTRIES = 4_096
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def no_follow_flag() -> int:
    """Return the platform's strongest no-follow open flag."""
    if sys.platform != "darwin":
        return os.O_NOFOLLOW
    no_follow_any = getattr(os, "O_NOFOLLOW_ANY", None)
    if type(no_follow_any) is not int:
        raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
    return no_follow_any


def path_metadata(path: Path) -> os.stat_result | None:
    """Return no-follow metadata, distinguishing only exact absence."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None


def existing_ancestor(path: Path) -> Path:
    """Return the nearest existing exact directory ancestor."""
    candidate = path
    while path_metadata(candidate) is None:
        parent = candidate.parent
        if parent == candidate:
            raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)
        candidate = parent
    metadata = path_metadata(candidate)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    return candidate


def close_descriptor(
    descriptor: int,
    primary: BaseException | None = None,
) -> None:
    """Close one descriptor without replacing an owned failure."""
    try:
        os.close(descriptor)
    except OSError:
        if primary is None:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        primary.add_note("Native descriptor cleanup also failed.")
    if primary is not None:
        raise primary from None


def close_descriptor_stack(
    descriptors: list[int],
    primary: BaseException | None = None,
) -> None:
    """Close a directory-descriptor chain in reverse ownership order."""
    failure = primary
    for descriptor in reversed(descriptors):
        try:
            close_descriptor(descriptor)
        except NativeFilesystemError as error:
            if failure is None:
                failure = error
            else:
                failure.add_note("Native descriptor cleanup also failed.")
    if failure is not None:
        raise failure from None


@contextmanager
def owned_descriptor(
    descriptor: int,
    failure_kind: NativeFailureKind,
) -> Iterator[int]:
    """Translate descriptor work and preserve owned cleanup failures."""
    primary: BaseException | None = None
    try:
        yield descriptor
    except NativeFilesystemError as error:
        primary = error
    except OSError:
        primary = NativeFilesystemError(failure_kind)
    except BaseException as error:
        primary = error
    try:
        os.close(descriptor)
    except OSError:
        if primary is not None:
            primary.add_note("Native descriptor cleanup also failed.")
        else:
            primary = NativeFilesystemError(NativeFailureKind.UNSAFE)
    if primary is not None:
        raise primary from None


def open_directory(path: Path, *, private: bool) -> int:
    """Open and validate one exact directory descriptor."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | no_follow_flag()
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        if not private and (
            metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
        if private and (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    except OSError:
        close_descriptor(
            descriptor,
            NativeFilesystemError(NativeFailureKind.UNSAFE),
        )
    except NativeFilesystemError as error:
        close_descriptor(descriptor, error)
    except BaseException as error:
        close_descriptor(descriptor, error)
    return descriptor


def open_child_directory(
    parent_descriptor: int,
    basename: str,
    *,
    private: bool,
) -> int:
    """Open one same-device child directory through its parent handle."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | no_follow_flag()
    try:
        descriptor = os.open(basename, flags, dir_fd=parent_descriptor)
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
    try:
        metadata = os.fstat(descriptor)
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != parent_metadata.st_dev
            or (
                not private
                and (
                    metadata.st_uid not in {0, os.geteuid()}
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                )
            )
            or (
                private
                and (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                )
            )
        ):
            raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    except OSError:
        close_descriptor(
            descriptor,
            NativeFilesystemError(NativeFailureKind.UNSAFE),
        )
    except NativeFilesystemError as error:
        close_descriptor(descriptor, error)
    return descriptor


def require_exact_entry(
    parent_descriptor: int,
    basename: str,
) -> tuple[int, int] | None:
    """Return an exact child's identity and reject case aliases."""
    exact = False
    alias = False
    requested = basename.casefold()
    try:
        with os.scandir(parent_descriptor) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_PROVIDER_DIRECTORY_ENTRIES:
                    raise NativeFilesystemError(
                        NativeFailureKind.TOO_LARGE
                    )
                if entry.name == basename:
                    exact = True
                elif entry.name.casefold() == requested:
                    alias = True
    except OSError:
        raise NativeFilesystemError(NativeFailureKind.UNREADABLE) from None
    if alias:
        raise NativeFilesystemError(NativeFailureKind.UNSAFE)
    if exact:
        try:
            metadata = os.stat(
                basename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise NativeFilesystemError(NativeFailureKind.CHANGED) from None
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        return metadata.st_dev, metadata.st_ino
    return None


def extend_parent_chain(
    descriptors: list[int],
    components: tuple[str, ...],
) -> None:
    """Create and open the private parent chain beneath one ancestor."""
    for index, component in enumerate(components):
        parent_descriptor = descriptors[-1]
        created = False
        try:
            os.mkdir(
                component,
                PRIVATE_DIRECTORY_MODE,
                dir_fd=parent_descriptor,
            )
            created = True
        except FileExistsError:
            pass
        except OSError:
            raise NativeFilesystemError(NativeFailureKind.CREATE) from None
        child = open_child_directory(
            parent_descriptor,
            component,
            private=index == len(components) - 1,
        )
        descriptors.append(child)
        if created:
            try:
                os.fsync(parent_descriptor)
            except OSError:
                raise NativeFilesystemError(
                    NativeFailureKind.SYNCHRONIZE
                ) from None
