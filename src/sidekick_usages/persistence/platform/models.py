"""Native persistence boundary models."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.persistence.platform.types import (
    FilesystemFamily,
    NativeIdentity,
    RelativePath,
)


@dataclass(frozen=True, slots=True)
class FilesystemQualification:
    """Approved filesystem family for one authority path."""

    family: FilesystemFamily
    authority_path: Path


@dataclass(frozen=True, slots=True)
class NativeFile:
    """Validated bounded bytes plus stable open-handle identity."""

    device: int
    inode: int
    link_count: int
    data: bytes = field(repr=False)
    modified_nanoseconds: int | None = field(
        default=None,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Reject invalid optional provider modification time."""
        if (
            self.modified_nanoseconds is not None
            and self.modified_nanoseconds < 0
        ):
            raise ValueError("Native file modification time is invalid.")


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One identity-qualified private-tree descendant."""

    relative: RelativePath
    identity: NativeIdentity
    directory: bool
