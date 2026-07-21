"""Validated evidence models for passive persistence assessment."""

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeDocument,
    PrototypeReceipt,
    VersionOneDocument,
    VersionTwoDocument,
)

_MAX_BASENAME_BYTES = 255


class StoredGeneration(StrEnum):
    """Generation known from passive authority evidence."""

    ABSENT = "absent"
    GENERATION_ZERO = "generation_zero"
    VERSION_ONE = "version_one"
    VERSION_TWO = "version_two"
    FUTURE = "future"
    UNKNOWN = "unknown"


class AuthorityKind(StrEnum):
    """Closed authority observations supplied by the filesystem boundary."""

    ABSENT = "absent"
    GENERATION_ZERO = "generation_zero"
    VERSION_ONE = "version_one"
    VERSION_TWO = "version_two"
    FUTURE = "future"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    INVALID_SCHEMA = "invalid_schema"
    UNREADABLE = "unreadable"
    UNSAFE = "unsafe"
    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"


class ArtifactKind(StrEnum):
    """Managed and import-only artifact categories."""

    LOCK = "lock"
    V0_BACKUP = "v0_backup"
    V1_SNAPSHOT = "v1_snapshot"
    V2_SNAPSHOT = "v2_snapshot"
    PROTOTYPE_RECEIPT = "prototype_receipt"
    TEMPORARY = "temporary"
    PROTOTYPE = "prototype"


class ArtifactState(StrEnum):
    """Security and decoding state of one observed artifact."""

    VALID = "valid"
    UNSAFE = "unsafe"
    UNREADABLE = "unreadable"
    BOUND_EXCEEDED = "bound_exceeded"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    FUTURE_SCHEMA = "future_schema"
    INVALID_SCHEMA = "invalid_schema"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class AuthorityObservation:
    """Validated or safely classified authoritative-path evidence."""

    kind: AuthorityKind
    content: bytes | None = field(default=None, repr=False)
    generation_zero: GenerationZeroDocument | None = field(
        default=None,
        repr=False,
    )
    version_one: VersionOneDocument | None = field(default=None, repr=False)
    version_two: VersionTwoDocument | None = field(default=None, repr=False)
    future_schema_version: int | None = None

    def __post_init__(self) -> None:
        documents = (
            self.generation_zero,
            self.version_one,
            self.version_two,
        )
        selected = {
            AuthorityKind.GENERATION_ZERO: self.generation_zero,
            AuthorityKind.VERSION_ONE: self.version_one,
            AuthorityKind.VERSION_TWO: self.version_two,
        }
        if self.kind in selected:
            valid = (
                self.content is not None
                and selected[self.kind] is not None
                and sum(document is not None for document in documents) == 1
                and self.future_schema_version is None
            )
        elif self.kind is AuthorityKind.FUTURE:
            valid = (
                type(self.future_schema_version) is int
                and self.content is None
                and all(document is None for document in documents)
            )
        else:
            valid = (
                self.content is None
                and all(document is None for document in documents)
                and self.future_schema_version is None
            )
        if not valid:
            raise ValueError("Authority observation fields disagree.")

    @property
    def generation(self) -> StoredGeneration:
        """Return the public generation derived from authority evidence."""
        return {
            AuthorityKind.ABSENT: StoredGeneration.ABSENT,
            AuthorityKind.GENERATION_ZERO: StoredGeneration.GENERATION_ZERO,
            AuthorityKind.VERSION_ONE: StoredGeneration.VERSION_ONE,
            AuthorityKind.VERSION_TWO: StoredGeneration.VERSION_TWO,
            AuthorityKind.FUTURE: StoredGeneration.FUTURE,
        }.get(self.kind, StoredGeneration.UNKNOWN)

    @property
    def schema_version(self) -> int | None:
        """Return a known envelope version without decoding again."""
        if self.kind is AuthorityKind.VERSION_ONE:
            return 1
        if self.kind is AuthorityKind.VERSION_TWO:
            return 2
        return self.future_schema_version

    @property
    def account_count(self) -> int | None:
        """Return a validated authoritative account count when known."""
        document = self.generation_zero or self.version_one or self.version_two
        return len(document.accounts) if document is not None else None


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    """One safe managed-artifact or prototype observation."""

    kind: ArtifactKind
    basename: str
    state: ArtifactState
    content: bytes | None = field(default=None, repr=False)
    generation_zero: GenerationZeroDocument | None = field(
        default=None,
        repr=False,
    )
    version_one: VersionOneDocument | None = field(default=None, repr=False)
    version_two: VersionTwoDocument | None = field(default=None, repr=False)
    prototype: PrototypeDocument | None = field(default=None, repr=False)
    receipt: PrototypeReceipt | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require_safe_observation_basename(self.basename)
        documents = self._documents()
        if self.state is ArtifactState.VALID:
            valid = self._valid_payload()
        else:
            valid = all(document is None for document in documents)
            readable_prototype = (
                self.kind is ArtifactKind.PROTOTYPE
                and self.state
                not in {
                    ArtifactState.UNSAFE,
                    ArtifactState.UNREADABLE,
                    ArtifactState.BOUND_EXCEEDED,
                }
            )
            valid = valid and (
                self.content is None
                if not readable_prototype
                else self.content is not None
            )
        if not valid:
            raise ValueError("Artifact observation fields disagree.")

    def _documents(self) -> tuple[object | None, ...]:
        return (
            self.generation_zero,
            self.version_one,
            self.version_two,
            self.prototype,
            self.receipt,
        )

    def _valid_payload(self) -> bool:
        documents = self._documents()
        payload_present = {
            ArtifactKind.V0_BACKUP: self.generation_zero is not None,
            ArtifactKind.V1_SNAPSHOT: self.version_one is not None,
            ArtifactKind.V2_SNAPSHOT: self.version_two is not None,
            ArtifactKind.PROTOTYPE: self.prototype is not None,
            ArtifactKind.PROTOTYPE_RECEIPT: self.receipt is not None,
        }
        if self.kind not in payload_present:
            return self.content is None and all(
                document is None for document in documents
            )
        expected_content = self.kind is not ArtifactKind.PROTOTYPE_RECEIPT
        return (
            payload_present[self.kind]
            and sum(document is not None for document in documents) == 1
            and (self.content is not None) is expected_content
        )


@dataclass(frozen=True, slots=True)
class PersistenceObservation:
    """Complete read-only evidence supplied to passive assessment."""

    safe_path: Path
    authority: AuthorityObservation
    artifacts: tuple[ArtifactObservation, ...] = ()
    orphaned_credentials: bool = False
    interrupted_credentials: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.orphaned_credentials) is not bool
            or type(self.interrupted_credentials) is not bool
        ):
            raise TypeError("Credential observations must be Boolean.")
        if self.orphaned_credentials and self.interrupted_credentials:
            raise ValueError("Credential observations cannot conflict.")
        basenames = tuple(artifact.basename for artifact in self.artifacts)
        if len(basenames) != len(set(basenames)):
            raise ValueError("Artifact basenames must be unique.")
        for singleton in (ArtifactKind.LOCK, ArtifactKind.PROTOTYPE):
            if (
                sum(artifact.kind is singleton for artifact in self.artifacts)
                > 1
            ):
                raise ValueError(f"Only one {singleton} may be observed.")


def require_safe_observation_basename(value: str) -> None:
    """Require one bounded basename safe for passive public output."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = b""
    if (
        not encoded
        or len(encoded) > _MAX_BASENAME_BYTES
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ValueError("Artifact observation requires a safe basename.")


__all__ = [
    "ArtifactKind",
    "ArtifactObservation",
    "ArtifactState",
    "AuthorityKind",
    "AuthorityObservation",
    "PersistenceObservation",
    "StoredGeneration",
    "require_safe_observation_basename",
]
