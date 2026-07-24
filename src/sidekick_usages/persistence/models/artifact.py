"""Persistence artifact identity and snapshot models."""

from dataclasses import dataclass, field

from sidekick_usages.persistence.types.artifact import (
    ArtifactPurpose,
    AuthorityExpectation,
    ManagedArtifactKind,
    Sha256Digest,
    sha256_digest,
)

type ExpectedAuthority = AuthorityExpectation | FileFingerprint


@dataclass(frozen=True, slots=True)
class ManagedArtifact:
    """One basename proven to match the closed managed grammar."""

    kind: ManagedArtifactKind
    basename: str
    purpose: ArtifactPurpose | None = None


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Stable identity obtained from an open final file handle."""

    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Identity and exact content fingerprint for source revalidation."""

    identity: FileIdentity
    digest: Sha256Digest
    size: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Protected bounded bytes and their verified fingerprint."""

    fingerprint: FileFingerprint
    link_count: int
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.fingerprint.size != len(self.data)
            or self.fingerprint.digest != sha256_digest(self.data)
            or self.link_count not in {1, 2}
        ):
            raise ValueError("Snapshot fingerprint does not match its bytes.")
