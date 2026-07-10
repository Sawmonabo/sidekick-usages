"""Deterministic passive persistence assessment and operation outcomes."""

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.migrations import (
    prototype_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeDocument,
    PrototypeReceipt,
    VersionOneDocument,
    encode_generation_zero,
    encode_version_one,
)


class PersistenceCode(StrEnum):
    """Closed passive and operation-time persistence outcomes."""

    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"
    UNSAFE_PERMISSIONS = "unsafe_permissions"
    UNREADABLE = "unreadable"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    FUTURE_SCHEMA = "future_schema"
    INVALID_SCHEMA = "invalid_schema"
    BACKUP_CONFLICT = "backup_conflict"
    INTERRUPTED_ARTIFACTS = "interrupted_artifacts"
    LEGACY_WRITER_DETECTED = "legacy_writer_detected"
    ROLLBACK_PREPARED = "rollback_prepared"
    MIGRATION_REQUIRED = "migration_required"
    PROTOTYPE_IMPORT_REQUIRED = "prototype_import_required"
    PROTOTYPE_IMPORTED = "prototype_imported"
    CURRENT = "current"
    EMPTY = "empty"
    ROLLBACK_REQUIRED = "rollback_required"
    STORE_LOCKED = "store_locked"
    SOURCE_CHANGED = "source_changed"
    REPLACE_FAILED = "replace_failed"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    RESET_INCOMPLETE = "reset_incomplete"


class StoredGeneration(StrEnum):
    """Generation known from passive authority evidence."""

    ABSENT = "absent"
    GENERATION_ZERO = "generation_zero"
    VERSION_ONE = "version_one"
    FUTURE = "future"
    UNKNOWN = "unknown"


class AuthorityKind(StrEnum):
    """Closed authority observations supplied by the filesystem boundary."""

    ABSENT = "absent"
    GENERATION_ZERO = "generation_zero"
    VERSION_ONE = "version_one"
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
    PROTOTYPE_RECEIPT = "prototype_receipt"
    TEMPORARY = "temporary"
    PROTOTYPE = "prototype"


class ArtifactState(StrEnum):
    """Security and decoding state of one observed artifact."""

    VALID = "valid"
    UNSAFE = "unsafe"
    UNREADABLE = "unreadable"
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
    future_schema_version: int | None = None

    def __post_init__(self) -> None:
        documents = (self.generation_zero, self.version_one)
        selected = {
            AuthorityKind.GENERATION_ZERO: self.generation_zero,
            AuthorityKind.VERSION_ONE: self.version_one,
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
            AuthorityKind.FUTURE: StoredGeneration.FUTURE,
        }.get(self.kind, StoredGeneration.UNKNOWN)

    @property
    def schema_version(self) -> int | None:
        """Return a known envelope version without decoding again."""
        if self.kind is AuthorityKind.VERSION_ONE:
            return 1
        return self.future_schema_version

    @property
    def account_count(self) -> int | None:
        """Return a validated authoritative account count when known."""
        document = self.generation_zero or self.version_one
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
    prototype: PrototypeDocument | None = field(default=None, repr=False)
    receipt: PrototypeReceipt | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_safe_basename(self.basename)
        documents = self._documents()
        if self.state is ArtifactState.VALID:
            valid = self._valid_payload()
        else:
            valid = all(document is None for document in documents)
            readable_prototype = (
                self.kind is ArtifactKind.PROTOTYPE
                and self.state
                not in {ArtifactState.UNSAFE, ArtifactState.UNREADABLE}
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
            self.prototype,
            self.receipt,
        )

    def _valid_payload(self) -> bool:
        documents = self._documents()
        payload_present = {
            ArtifactKind.V0_BACKUP: self.generation_zero is not None,
            ArtifactKind.V1_SNAPSHOT: self.version_one is not None,
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

    def __post_init__(self) -> None:
        if type(self.orphaned_credentials) is not bool:
            raise TypeError("orphaned_credentials must be Boolean.")
        basenames = tuple(artifact.basename for artifact in self.artifacts)
        if len(basenames) != len(set(basenames)):
            raise ValueError("Artifact basenames must be unique.")
        for singleton in (ArtifactKind.LOCK, ArtifactKind.PROTOTYPE):
            if (
                sum(artifact.kind is singleton for artifact in self.artifacts)
                > 1
            ):
                raise ValueError(f"Only one {singleton} may be observed.")


@dataclass(frozen=True, slots=True)
class PersistenceIssue:
    """One safe deterministic passive persistence finding."""

    code: PersistenceCode
    artifact_basename: str | None
    message: str


@dataclass(frozen=True, slots=True)
class PersistenceAssessment:
    """Deterministic public state derived from passive evidence."""

    code: PersistenceCode
    generation: StoredGeneration
    schema_version: int | None
    account_count: int | None
    safe_path: Path
    artifact_basename: str | None
    write_blocked: bool
    next_command: tuple[str, ...] | None
    message: str
    issues: tuple[PersistenceIssue, ...]


@dataclass(frozen=True, slots=True)
class PersistenceOperationResult:
    """One transient operation outcome plus freshly observed state."""

    code: PersistenceCode
    assessment: PersistenceAssessment
    artifact_basename: str | None
    message: str


@dataclass(frozen=True, slots=True)
class _RankedIssue:
    issue: PersistenceIssue
    artifact_rank: int


_PASSIVE_PRIORITY: dict[PersistenceCode, int] = {
    PersistenceCode.UNSUPPORTED_FILESYSTEM: 10,
    PersistenceCode.UNSAFE_PERMISSIONS: 20,
    PersistenceCode.UNREADABLE: 30,
    PersistenceCode.DUPLICATE_KEY: 40,
    PersistenceCode.MALFORMED_JSON: 50,
    PersistenceCode.FUTURE_SCHEMA: 60,
    PersistenceCode.INVALID_SCHEMA: 70,
    PersistenceCode.BACKUP_CONFLICT: 80,
    PersistenceCode.INTERRUPTED_ARTIFACTS: 90,
    PersistenceCode.LEGACY_WRITER_DETECTED: 100,
    PersistenceCode.ROLLBACK_PREPARED: 110,
    PersistenceCode.MIGRATION_REQUIRED: 120,
    PersistenceCode.PROTOTYPE_IMPORT_REQUIRED: 130,
    PersistenceCode.PROTOTYPE_IMPORTED: 140,
    PersistenceCode.CURRENT: 150,
    PersistenceCode.EMPTY: 160,
}

_ARTIFACT_RANK: dict[ArtifactKind, int] = {
    ArtifactKind.LOCK: 1,
    ArtifactKind.V0_BACKUP: 2,
    ArtifactKind.V1_SNAPSHOT: 3,
    ArtifactKind.TEMPORARY: 4,
    ArtifactKind.PROTOTYPE_RECEIPT: 5,
    ArtifactKind.PROTOTYPE: 6,
}

_MAX_BASENAME_BYTES = 255

_MESSAGE: dict[PersistenceCode, str] = {
    PersistenceCode.UNSUPPORTED_FILESYSTEM: "Filesystem is unsupported.",
    PersistenceCode.UNSAFE_PERMISSIONS: "A persistence object is unsafe.",
    PersistenceCode.UNREADABLE: "A persistence object is unreadable.",
    PersistenceCode.DUPLICATE_KEY: "Account JSON repeats a member.",
    PersistenceCode.MALFORMED_JSON: "Account data is not strict JSON.",
    PersistenceCode.FUTURE_SCHEMA: "Compatible software is required.",
    PersistenceCode.INVALID_SCHEMA: "Account data violates its schema.",
    PersistenceCode.BACKUP_CONFLICT: "A managed backup conflicts.",
    PersistenceCode.INTERRUPTED_ARTIFACTS: "Persistence is incomplete.",
    PersistenceCode.LEGACY_WRITER_DETECTED: "A legacy writer changed state.",
    PersistenceCode.ROLLBACK_PREPARED: "A rollback snapshot matches.",
    PersistenceCode.MIGRATION_REQUIRED: "Account data requires migration.",
    PersistenceCode.PROTOTYPE_IMPORT_REQUIRED: "Prototype import is required.",
    PersistenceCode.PROTOTYPE_IMPORTED: "The prototype import matches.",
    PersistenceCode.CURRENT: "Account data uses the current schema.",
    PersistenceCode.EMPTY: "No account data is present.",
    PersistenceCode.ROLLBACK_REQUIRED: "Rollback preparation is required.",
    PersistenceCode.STORE_LOCKED: "Another process holds the store lock.",
    PersistenceCode.SOURCE_CHANGED: "The authoritative source changed.",
    PersistenceCode.REPLACE_FAILED: "Store replacement failed.",
    PersistenceCode.DURABILITY_UNCERTAIN: "Store durability is uncertain.",
    PersistenceCode.RESET_INCOMPLETE: "Reset left credential artifacts.",
}

_MIGRATE_COMMAND = ("sidekick-usages", "migrate", "accounts")
_PASSIVE_SUCCESS = frozenset(
    {
        PersistenceCode.EMPTY,
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
        PersistenceCode.ROLLBACK_PREPARED,
    }
)
_PASSIVE_MANUAL = frozenset(
    {
        PersistenceCode.MIGRATION_REQUIRED,
        PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
        PersistenceCode.LEGACY_WRITER_DETECTED,
        PersistenceCode.INTERRUPTED_ARTIFACTS,
        PersistenceCode.FUTURE_SCHEMA,
    }
)
_OPERATION_SUCCESS = frozenset(
    {
        PersistenceCode.PROTOTYPE_IMPORTED,
        PersistenceCode.ROLLBACK_PREPARED,
    }
)
_OPERATION_MANUAL = frozenset(
    {
        PersistenceCode.ROLLBACK_REQUIRED,
        PersistenceCode.STORE_LOCKED,
        PersistenceCode.SOURCE_CHANGED,
        PersistenceCode.LEGACY_WRITER_DETECTED,
        PersistenceCode.INTERRUPTED_ARTIFACTS,
    }
)


def _validate_safe_basename(value: str) -> None:
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


def _ranked_issue(
    code: PersistenceCode,
    *,
    artifact: ArtifactObservation | None = None,
    artifact_rank: int = 0,
) -> _RankedIssue:
    basename = artifact.basename if artifact is not None else None
    rank = (
        _ARTIFACT_RANK[artifact.kind]
        if artifact is not None
        else artifact_rank
    )
    return _RankedIssue(PersistenceIssue(code, basename, _MESSAGE[code]), rank)


def _artifact_failure_issue(
    artifact: ArtifactObservation,
) -> _RankedIssue | None:
    if artifact.state is ArtifactState.VALID:
        return None
    if artifact.state is ArtifactState.UNSAFE:
        code = PersistenceCode.UNSAFE_PERMISSIONS
    elif artifact.state is ArtifactState.UNREADABLE:
        code = PersistenceCode.UNREADABLE
    elif artifact.kind in {
        ArtifactKind.V0_BACKUP,
        ArtifactKind.V1_SNAPSHOT,
    }:
        code = PersistenceCode.BACKUP_CONFLICT
    elif artifact.kind is ArtifactKind.PROTOTYPE:
        if artifact.state is ArtifactState.DUPLICATE_KEY:
            code = PersistenceCode.DUPLICATE_KEY
        elif artifact.state is ArtifactState.MALFORMED_JSON:
            code = PersistenceCode.MALFORMED_JSON
        else:
            code = PersistenceCode.INVALID_SCHEMA
    else:
        code = PersistenceCode.INVALID_SCHEMA
    return _ranked_issue(code, artifact=artifact)


def _authority_failure_issue(
    authority: AuthorityObservation,
) -> _RankedIssue | None:
    code = {
        AuthorityKind.UNSUPPORTED_FILESYSTEM: (
            PersistenceCode.UNSUPPORTED_FILESYSTEM
        ),
        AuthorityKind.UNSAFE: PersistenceCode.UNSAFE_PERMISSIONS,
        AuthorityKind.UNREADABLE: PersistenceCode.UNREADABLE,
        AuthorityKind.DUPLICATE_KEY: PersistenceCode.DUPLICATE_KEY,
        AuthorityKind.MALFORMED_JSON: PersistenceCode.MALFORMED_JSON,
        AuthorityKind.FUTURE: PersistenceCode.FUTURE_SCHEMA,
        AuthorityKind.INVALID_SCHEMA: PersistenceCode.INVALID_SCHEMA,
    }.get(authority.kind)
    return _ranked_issue(code) if code is not None else None


def _artifacts_of_kind(
    artifacts: tuple[ArtifactObservation, ...],
    kind: ArtifactKind,
) -> tuple[ArtifactObservation, ...]:
    return tuple(artifact for artifact in artifacts if artifact.kind is kind)


def _matching_prototype_receipt(
    prototype: ArtifactObservation,
    receipts: tuple[ArtifactObservation, ...],
) -> bool:
    if prototype.content is None:
        return False
    digest = hashlib.sha256(prototype.content).hexdigest()
    return any(
        receipt.state is ArtifactState.VALID
        and receipt.receipt is not None
        and receipt.receipt.prototype_sha256 == digest
        for receipt in receipts
    )


def _prototype_matches_authority(
    prototype: ArtifactObservation,
    receipts: tuple[ArtifactObservation, ...],
    authority: AuthorityObservation,
) -> bool:
    if (
        prototype.state is not ArtifactState.VALID
        or prototype.prototype is None
        or authority.content is None
        or not _matching_prototype_receipt(prototype, receipts)
    ):
        return False
    try:
        encoded = encode_version_one(
            prototype_to_version_one(prototype.prototype)
        )
    except InvalidSchemaError:
        return False
    return encoded == authority.content


def _snapshot_reverses_to_authority(
    snapshot: ArtifactObservation,
    authority: AuthorityObservation,
) -> bool:
    if snapshot.version_one is None or authority.content is None:
        return False
    try:
        reversed_document = version_one_to_v060(snapshot.version_one)
        encoded = encode_generation_zero(reversed_document)
    except InvalidSchemaError, RollbackCompatibilityError:
        return False
    return encoded == authority.content


type _LogicalResult = tuple[_RankedIssue, int | None, bool]


def _generation_zero_issue(
    authority: AuthorityObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> _LogicalResult:
    snapshots = tuple(
        artifact
        for artifact in _artifacts_of_kind(
            artifacts,
            ArtifactKind.V1_SNAPSHOT,
        )
        if artifact.state is ArtifactState.VALID
    )
    if any(
        _snapshot_reverses_to_authority(snapshot, authority)
        for snapshot in snapshots
    ):
        code = PersistenceCode.ROLLBACK_PREPARED
    elif snapshots:
        code = PersistenceCode.LEGACY_WRITER_DETECTED
    else:
        code = PersistenceCode.MIGRATION_REQUIRED
    return _ranked_issue(code), authority.account_count, False


def _version_one_issue(
    authority: AuthorityObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> _LogicalResult:
    prototype = next(
        iter(_artifacts_of_kind(artifacts, ArtifactKind.PROTOTYPE)),
        None,
    )
    receipts = _artifacts_of_kind(
        artifacts,
        ArtifactKind.PROTOTYPE_RECEIPT,
    )
    imported = prototype is not None and _prototype_matches_authority(
        prototype,
        receipts,
        authority,
    )
    code = (
        PersistenceCode.PROTOTYPE_IMPORTED
        if imported
        else PersistenceCode.CURRENT
    )
    return _ranked_issue(code), authority.account_count, False


def _absent_issue(
    observation: PersistenceObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> _LogicalResult:
    recovery_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.kind
        in {
            ArtifactKind.V0_BACKUP,
            ArtifactKind.V1_SNAPSHOT,
            ArtifactKind.TEMPORARY,
        }
    )
    if recovery_artifacts or observation.orphaned_credentials:
        first = min(
            recovery_artifacts,
            key=lambda item: (_ARTIFACT_RANK[item.kind], item.basename),
            default=None,
        )
        return (
            _ranked_issue(
                PersistenceCode.INTERRUPTED_ARTIFACTS,
                artifact=first,
                artifact_rank=4,
            ),
            None,
            False,
        )

    prototype = next(
        iter(_artifacts_of_kind(artifacts, ArtifactKind.PROTOTYPE)),
        None,
    )
    if prototype is None:
        return _ranked_issue(PersistenceCode.EMPTY), 0, False
    receipts = _artifacts_of_kind(
        artifacts,
        ArtifactKind.PROTOTYPE_RECEIPT,
    )
    if _matching_prototype_receipt(prototype, receipts):
        return _ranked_issue(PersistenceCode.EMPTY), 0, False
    if prototype.state is not ArtifactState.VALID:
        failure = _artifact_failure_issue(prototype)
        if failure is None:
            raise ValueError("Unhandled prototype observation.")
        return failure, None, False
    return (
        _ranked_issue(
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            artifact=prototype,
        ),
        len(prototype.prototype.accounts)
        if prototype.prototype is not None
        else None,
        any(receipt.state is ArtifactState.VALID for receipt in receipts),
    )


def _logical_issue(
    observation: PersistenceObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> _LogicalResult:
    authority = observation.authority
    if authority.kind is AuthorityKind.GENERATION_ZERO:
        return _generation_zero_issue(authority, artifacts)
    if authority.kind is AuthorityKind.VERSION_ONE:
        return _version_one_issue(authority, artifacts)
    if authority.kind is AuthorityKind.ABSENT:
        return _absent_issue(observation, artifacts)
    failure = _authority_failure_issue(authority)
    if failure is None:
        raise ValueError("Unhandled authority observation.")
    return failure, None, False


def _interruption_issues(
    authority: AuthorityObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> list[_RankedIssue]:
    kinds = {ArtifactKind.TEMPORARY}
    if authority.kind is AuthorityKind.ABSENT:
        kinds.update((ArtifactKind.V0_BACKUP, ArtifactKind.V1_SNAPSHOT))
    return [
        _ranked_issue(
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            artifact=artifact,
        )
        for artifact in artifacts
        if artifact.kind in kinds
    ]


def _ordered_issues(
    issues: Iterable[_RankedIssue],
) -> tuple[PersistenceIssue, ...]:
    unique = {
        (ranked.issue.code, ranked.issue.artifact_basename): ranked
        for ranked in issues
    }
    ordered = sorted(
        unique.values(),
        key=lambda ranked: (
            _PASSIVE_PRIORITY[ranked.issue.code],
            ranked.artifact_rank,
            ranked.issue.artifact_basename or "",
        ),
    )
    return tuple(ranked.issue for ranked in ordered)


def _next_command(
    code: PersistenceCode,
    *,
    reimport_prototype: bool,
) -> tuple[str, ...] | None:
    if code in {
        PersistenceCode.MIGRATION_REQUIRED,
        PersistenceCode.LEGACY_WRITER_DETECTED,
    }:
        return _MIGRATE_COMMAND
    if code is PersistenceCode.PROTOTYPE_IMPORT_REQUIRED:
        if reimport_prototype:
            return (*_MIGRATE_COMMAND, "--reimport-prototype")
        return _MIGRATE_COMMAND
    return None


def assess_persistence(
    observation: PersistenceObservation,
) -> PersistenceAssessment:
    """Reduce safe filesystem observations to deterministic passive state."""
    authority = observation.authority
    if authority.kind is AuthorityKind.UNSUPPORTED_FILESYSTEM:
        ranked_issues = [_ranked_issue(PersistenceCode.UNSUPPORTED_FILESYSTEM)]
        account_count = None
        reimport = False
    else:
        artifacts = tuple(
            sorted(observation.artifacts, key=lambda item: item.basename)
        )
        logical, account_count, reimport = _logical_issue(
            observation,
            artifacts,
        )
        ranked_issues = [logical]
        for artifact in artifacts:
            if artifact.kind is ArtifactKind.PROTOTYPE:
                continue
            if (failure := _artifact_failure_issue(artifact)) is not None:
                ranked_issues.append(failure)
        ranked_issues.extend(_interruption_issues(authority, artifacts))
    issues = _ordered_issues(ranked_issues)
    primary = issues[0]
    code = primary.code
    return PersistenceAssessment(
        code=code,
        generation=authority.generation,
        schema_version=authority.schema_version,
        account_count=account_count,
        safe_path=observation.safe_path,
        artifact_basename=primary.artifact_basename,
        write_blocked=code
        not in {
            PersistenceCode.EMPTY,
            PersistenceCode.CURRENT,
            PersistenceCode.PROTOTYPE_IMPORTED,
        },
        next_command=_next_command(code, reimport_prototype=reimport),
        message=primary.message,
        issues=issues,
    )


def passive_priority(code: PersistenceCode) -> int:
    """Return the fixed passive precedence for ``code``.

    :raises ValueError: If ``code`` is transient operation state.
    """
    try:
        return _PASSIVE_PRIORITY[code]
    except KeyError:
        raise ValueError(
            "Operation-only persistence code has no passive priority."
        ) from None


def doctor_exit_code(code: PersistenceCode) -> ExitCode:
    """Map passive assessment to the documented doctor process outcome."""
    if code in _PASSIVE_SUCCESS:
        return ExitCode.SUCCESS
    if code in _PASSIVE_MANUAL:
        return ExitCode.MANUAL_ACTION
    if code in _PASSIVE_PRIORITY:
        return ExitCode.SYSTEM_ERROR
    raise ValueError("Operation-only persistence code has no doctor mapping.")


def operation_exit_code(code: PersistenceCode) -> ExitCode:
    """Map a transient persistence operation to its process outcome."""
    if code in _OPERATION_SUCCESS:
        return ExitCode.SUCCESS
    if code in _OPERATION_MANUAL:
        return ExitCode.MANUAL_ACTION
    if code in {
        PersistenceCode.REPLACE_FAILED,
        PersistenceCode.DURABILITY_UNCERTAIN,
        PersistenceCode.RESET_INCOMPLETE,
        PersistenceCode.BACKUP_CONFLICT,
    }:
        return ExitCode.SYSTEM_ERROR
    raise ValueError("Passive-only persistence code is not an operation.")


def make_operation_result(
    code: PersistenceCode,
    assessment: PersistenceAssessment,
    *,
    artifact_basename: str | None = None,
) -> PersistenceOperationResult:
    """Create a safe operation result without replacing passive state."""
    operation_exit_code(code)
    if artifact_basename is not None:
        _validate_safe_basename(artifact_basename)
    return PersistenceOperationResult(
        code,
        assessment,
        artifact_basename,
        _MESSAGE[code],
    )
