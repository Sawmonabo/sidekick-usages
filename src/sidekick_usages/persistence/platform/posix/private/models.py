"""POSIX private-tree traversal models."""

from dataclasses import dataclass

from sidekick_usages.persistence.platform.types import (
    NativeIdentity,
    RelativePath,
)


@dataclass(frozen=True, slots=True)
class OpenedTree:
    """Held descriptors and identity for one private-tree root."""

    parent_descriptor: int
    root_descriptor: int
    root_identity: NativeIdentity
    root_device: int
    root_basename: str


@dataclass(slots=True)
class OpenedChain:
    """Held descriptors and identities for one root-relative chain."""

    descriptors: list[int]
    identities: tuple[NativeIdentity, ...]
    components: RelativePath


@dataclass(frozen=True, slots=True)
class RepairDirectory:
    """One identity-qualified directory eligible for mode repair."""

    relative: RelativePath
    identity: NativeIdentity
    mode: int
