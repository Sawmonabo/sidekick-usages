"""Native persistence boundary models."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.persistence.platform.types import FilesystemFamily


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
