"""Closed values for persistence artifact ownership."""

import hashlib
import re
from enum import StrEnum
from typing import Self

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class Sha256Digest(str):
    """Validated lowercase SHA-256 text."""

    def __new__(cls, value: str) -> Self:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Digest must be 64 lowercase hexadecimal bytes.")
        return str.__new__(cls, value)


class ArtifactPurpose(StrEnum):
    """Closed purpose vocabulary for owned temporary files."""

    AUTHORITY = "authority"


class ManagedArtifactKind(StrEnum):
    """Exact sibling artifacts owned by current persistence."""

    AUTHORITY = "authority"
    LOCK = "lock"
    TEMPORARY = "temporary"


class AuthorityExpectation(StrEnum):
    """Explicit first-write expectation for an absent authority."""

    ABSENT = "absent"


def sha256_digest(data: bytes) -> Sha256Digest:
    """Return the lowercase SHA-256 identity for exact bytes."""
    return Sha256Digest(hashlib.sha256(data).hexdigest())
