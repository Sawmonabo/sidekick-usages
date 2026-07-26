"""Windows private-tree traversal models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.platform.types import (
    NativeIdentity,
    RelativePath,
)


@dataclass(frozen=True, slots=True)
class OpenedTree:
    """Held descriptors and identity for one private-tree root."""

    root_path: Path
    parent_descriptor: int
    root_descriptor: int
    root_identity: NativeIdentity
    root_device: int
    root_basename: str


@dataclass(slots=True)
class OpenedChain:
    """Held descriptors and identities for one root-relative chain."""

    paths: tuple[Path, ...]
    descriptors: list[int]
    identities: tuple[NativeIdentity, ...]
    components: RelativePath


@dataclass(frozen=True, slots=True)
class RepairEntry:
    """One validated Windows descendant eligible for security repair."""

    relative: RelativePath
    identity: NativeIdentity
    directory: bool
    security_valid: bool
