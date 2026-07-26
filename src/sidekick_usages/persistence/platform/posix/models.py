"""POSIX private-tree operation models."""

from dataclasses import dataclass

from sidekick_usages.persistence.platform.types import (
    NativeIdentity,
    RelativePath,
)


@dataclass(frozen=True, slots=True)
class ProviderStageEntry:
    """One identity-qualified provider-stage descendant."""

    relative: RelativePath
    identity: NativeIdentity
    directory: bool
    mode: int
