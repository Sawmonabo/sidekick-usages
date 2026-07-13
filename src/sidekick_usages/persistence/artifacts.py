"""Closed names and content identities for persistence artifacts."""

import hashlib
import ntpath
import re
import secrets
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TEMPORARY_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


class Sha256Digest(str):
    """Validated lowercase SHA-256 text."""

    def __new__(cls, value: str) -> Sha256Digest:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Digest must be 64 lowercase hexadecimal bytes.")
        return str.__new__(cls, value)


class ArtifactPurpose(StrEnum):
    """Closed purpose vocabulary for owned temporary files."""

    AUTHORITY = "authority"
    BACKUP = "backup"
    SNAPSHOT = "snapshot"
    RECEIPT = "receipt"


class AuthorityGeneration(StrEnum):
    """Stored authority generations accepted by durable commit."""

    GENERATION_ZERO = "v0"
    VERSION_ONE = "v1"
    VERSION_TWO = "v2"


class ManagedArtifactKind(StrEnum):
    """Exact sibling artifact kinds owned by account persistence."""

    AUTHORITY = "authority"
    LOCK = "lock"
    GENERATION_ZERO_BACKUP = "generation_zero_backup"
    VERSION_ONE_SNAPSHOT = "version_one_snapshot"
    VERSION_TWO_SNAPSHOT = "version_two_snapshot"
    PROTOTYPE_RECEIPT = "prototype_receipt"
    TEMPORARY = "temporary"


@dataclass(frozen=True, slots=True)
class ManagedArtifact:
    """One basename proven to match the closed managed grammar."""

    kind: ManagedArtifactKind
    basename: str
    digest: Sha256Digest | None = None
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


class AuthorityExpectation(StrEnum):
    """Explicit first-write expectation for an absent authority."""

    ABSENT = "absent"


type ExpectedAuthority = AuthorityExpectation | FileFingerprint


@dataclass(frozen=True, slots=True)
class ArtifactGrammar:
    """Closed managed-name grammar derived from one authority basename."""

    authority_basename: str
    _v0_pattern: re.Pattern[str] = field(init=False, repr=False)
    _v1_pattern: re.Pattern[str] = field(init=False, repr=False)
    _v2_pattern: re.Pattern[str] = field(init=False, repr=False)
    _receipt_pattern: re.Pattern[str] = field(init=False, repr=False)
    _temporary_pattern: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        require_safe_basename(self.authority_basename)
        escaped = re.escape(self.authority_basename)
        object.__setattr__(
            self,
            "_v0_pattern",
            re.compile(rf"{escaped}\.v0\.([0-9a-f]{{64}})\.bak\Z"),
        )
        object.__setattr__(
            self,
            "_v1_pattern",
            re.compile(rf"{escaped}\.v1\.([0-9a-f]{{64}})\.bak\Z"),
        )
        object.__setattr__(
            self,
            "_v2_pattern",
            re.compile(rf"{escaped}\.v2\.([0-9a-f]{{64}})\.bak\Z"),
        )
        object.__setattr__(
            self,
            "_receipt_pattern",
            re.compile(
                rf"{escaped}\.prototype\.([0-9a-f]{{64}})"
                rf"\.receipt\Z"
            ),
        )
        purposes = "|".join(purpose.value for purpose in ArtifactPurpose)
        object.__setattr__(
            self,
            "_temporary_pattern",
            re.compile(
                rf"\.{escaped}\.({purposes})\.([0-9a-f]{{32}})"
                rf"\.tmp\Z"
            ),
        )

    @property
    def lock_basename(self) -> str:
        """Return the one persistent lock-sidecar basename."""
        return f"{self.authority_basename}.lock"

    def backup_basename(
        self,
        generation: AuthorityGeneration,
        digest: Sha256Digest,
    ) -> str:
        """Return a content-addressed immutable backup basename."""
        suffix = generation.value
        return f"{self.authority_basename}.{suffix}.{digest}.bak"

    def receipt_basename(self, prototype_digest: Sha256Digest) -> str:
        """Return the receipt name keyed by exact prototype bytes."""
        return (
            f"{self.authority_basename}.prototype.{prototype_digest}.receipt"
        )

    def temporary_basename(self, purpose: ArtifactPurpose) -> str:
        """Return a fresh 128-bit temporary basename."""
        return (
            f".{self.authority_basename}.{purpose.value}."
            f"{secrets.token_hex(16)}.tmp"
        )

    def parse(self, basename: str) -> ManagedArtifact | None:
        """Classify one exact basename without opening it."""
        if not is_safe_basename(basename):
            return None
        if basename == self.authority_basename:
            return ManagedArtifact(
                ManagedArtifactKind.AUTHORITY,
                basename,
            )
        if basename == self.lock_basename:
            return ManagedArtifact(ManagedArtifactKind.LOCK, basename)
        return self._parse_content_addressed(
            basename
        ) or self._parse_temporary(basename)

    def _parse_content_addressed(
        self,
        basename: str,
    ) -> ManagedArtifact | None:
        patterns = (
            (
                self._v0_pattern,
                ManagedArtifactKind.GENERATION_ZERO_BACKUP,
            ),
            (
                self._v1_pattern,
                ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ),
            (
                self._v2_pattern,
                ManagedArtifactKind.VERSION_TWO_SNAPSHOT,
            ),
            (
                self._receipt_pattern,
                ManagedArtifactKind.PROTOTYPE_RECEIPT,
            ),
        )
        for pattern, kind in patterns:
            if (match := pattern.fullmatch(basename)) is not None:
                return ManagedArtifact(
                    kind,
                    basename,
                    digest=Sha256Digest(match.group(1)),
                )
        return None

    def _parse_temporary(self, basename: str) -> ManagedArtifact | None:
        match = self._temporary_pattern.fullmatch(basename)
        if (
            match is None
            or _TEMPORARY_TOKEN_PATTERN.fullmatch(match.group(2)) is None
        ):
            return None
        return ManagedArtifact(
            ManagedArtifactKind.TEMPORARY,
            basename,
            purpose=ArtifactPurpose(match.group(1)),
        )


def is_safe_basename(value: str) -> bool:
    """Return whether one injected basename is safe for relative use."""
    return (
        bool(value)
        and value not in {".", ".."}
        and value == value.rstrip(" .")
        and not (
            "/" in value
            or "\\" in value
            or any(
                unicodedata.category(character) == "Cc" for character in value
            )
        )
    )


def portable_basename_key(value: str) -> str:
    """Return the Windows-compatible identity of one safe basename."""
    return ntpath.normcase(value.rstrip(" ."))


def require_portable_unique_basenames(values: Iterable[str]) -> None:
    """Reject names that alias in a portable filesystem namespace."""
    keys = tuple(portable_basename_key(value) for value in values)
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Artifact names must be unique in the portable namespace."
        )


def require_safe_basename(value: str) -> None:
    """Reject a basename that can escape or confuse the parent namespace."""
    if not is_safe_basename(value):
        raise ValueError("Authority path must have one safe basename.")


def sha256_digest(data: bytes) -> Sha256Digest:
    """Return the lowercase SHA-256 identity for exact bytes."""
    return Sha256Digest(hashlib.sha256(data).hexdigest())


__all__ = [
    "ArtifactGrammar",
    "ArtifactPurpose",
    "AuthorityExpectation",
    "AuthorityGeneration",
    "ExpectedAuthority",
    "FileFingerprint",
    "FileIdentity",
    "FileSnapshot",
    "ManagedArtifact",
    "ManagedArtifactKind",
    "Sha256Digest",
    "is_safe_basename",
    "portable_basename_key",
    "require_portable_unique_basenames",
    "require_safe_basename",
    "sha256_digest",
]
