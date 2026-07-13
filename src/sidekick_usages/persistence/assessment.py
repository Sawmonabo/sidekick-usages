"""Deterministic passive persistence assessment and operation outcomes."""

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    PersistenceCode,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.observations import (
    ArtifactKind,
    ArtifactObservation,
    ArtifactState,
    AuthorityKind,
    AuthorityObservation,
    PersistenceObservation,
    StoredGeneration,
    require_safe_observation_basename,
)
from sidekick_usages.persistence.schemas import (
    encode_generation_zero,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    prototype_to_version_two,
    version_two_to_v060,
)


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
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class PersistenceOperationResult:
    """One transient operation outcome plus freshly observed state."""

    code: PersistenceCode
    assessment: PersistenceAssessment
    artifact_basename: str | None
    message: str


@dataclass(frozen=True, slots=True)
class PersistenceCompositionFailure:
    """Safe passive filesystem failure captured during app composition."""

    code: PersistenceCode
    safe_path: Path
    artifact_basename: str | None
    message: str
    next_command: tuple[str, ...] | None = None
    guidance: str | None = None

    def __post_init__(self) -> None:
        if self.code not in {
            PersistenceCode.UNSUPPORTED_FILESYSTEM,
            PersistenceCode.UNSAFE_PERMISSIONS,
            PersistenceCode.UNREADABLE,
        }:
            raise ValueError(
                "Composition failure requires a passive filesystem code."
            )
        if not self.safe_path.is_absolute():
            raise ValueError("Composition failure path must be absolute.")
        if self.artifact_basename is not None:
            require_safe_observation_basename(self.artifact_basename)
        if self.next_command is not None and not self.next_command:
            raise ValueError("Composition next command cannot be empty.")


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
    ArtifactKind.V2_SNAPSHOT: 4,
    ArtifactKind.TEMPORARY: 5,
    ArtifactKind.PROTOTYPE_RECEIPT: 6,
    ArtifactKind.PROTOTYPE: 7,
}

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
_PERMISSIONS_REPAIR_COMMAND = (
    "sidekick-usages",
    "permissions",
    "repair",
)
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
        ArtifactKind.V2_SNAPSHOT,
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
        encoded = encode_version_two(
            prototype_to_version_two(prototype.prototype)
        )
    except InvalidSchemaError:
        return False
    return encoded == authority.content


def _snapshot_reverses_to_authority(
    snapshot: ArtifactObservation,
    authority: AuthorityObservation,
) -> bool:
    if snapshot.version_two is None or authority.content is None:
        return False
    try:
        reversed_document = version_two_to_v060(snapshot.version_two)
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
            ArtifactKind.V2_SNAPSHOT,
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
    del artifacts
    return (
        _ranked_issue(PersistenceCode.MIGRATION_REQUIRED),
        authority.account_count,
        False,
    )


def _version_two_issue(
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
            ArtifactKind.V2_SNAPSHOT,
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
    if authority.kind is AuthorityKind.VERSION_TWO:
        return _version_two_issue(authority, artifacts)
    if authority.kind is AuthorityKind.ABSENT:
        return _absent_issue(observation, artifacts)
    failure = _authority_failure_issue(authority)
    if failure is None:
        raise ValueError("Unhandled authority observation.")
    return failure, None, False


def _interruption_issues(
    observation: PersistenceObservation,
    artifacts: tuple[ArtifactObservation, ...],
) -> list[_RankedIssue]:
    authority = observation.authority
    kinds = {ArtifactKind.TEMPORARY}
    if authority.kind is AuthorityKind.ABSENT:
        kinds.update(
            (
                ArtifactKind.V0_BACKUP,
                ArtifactKind.V1_SNAPSHOT,
                ArtifactKind.V2_SNAPSHOT,
            )
        )
    issues = [
        _ranked_issue(
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            artifact=artifact,
        )
        for artifact in artifacts
        if artifact.kind in kinds
    ]
    if observation.interrupted_credentials:
        issues.append(
            _ranked_issue(
                PersistenceCode.INTERRUPTED_ARTIFACTS,
                artifact_rank=4,
            )
        )
    return issues


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


def recovery_next_command(
    code: PersistenceCode,
    *,
    reimport_prototype: bool = False,
) -> tuple[str, ...] | None:
    """Return the structured safe recovery command for passive state."""
    if code is PersistenceCode.UNSAFE_PERMISSIONS:
        return _PERMISSIONS_REPAIR_COMMAND
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


def recovery_guidance(code: PersistenceCode) -> str | None:
    """Return bounded platform guidance for one recoverable passive state."""
    if code is not PersistenceCode.UNSAFE_PERMISSIONS:
        return None
    if os.name == "nt":
        return (
            "Sidekick can restore its exact protected Windows DACL without "
            "changing credential bytes."
        )
    return (
        "Sidekick can restore owner-only POSIX directory modes without "
        "changing credential bytes."
    )


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
        ranked_issues.extend(_interruption_issues(observation, artifacts))
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
        next_command=recovery_next_command(
            code,
            reimport_prototype=reimport,
        ),
        message=primary.message,
        issues=issues,
        guidance=recovery_guidance(code),
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
        require_safe_observation_basename(artifact_basename)
    return PersistenceOperationResult(
        code,
        assessment,
        artifact_basename,
        _MESSAGE[code],
    )
