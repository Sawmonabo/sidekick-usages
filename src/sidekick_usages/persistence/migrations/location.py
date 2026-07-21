"""Pure contracts for persistence-location selection and migration."""

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeIs

from sidekick_usages.persistence.artifacts import Sha256Digest
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
    passive_priority,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationAssessment,
    PrivateAuthMigrationFailure,
)
from sidekick_usages.persistence.observations import (
    require_safe_observation_basename,
)

_MIGRATE_ACCOUNTS_COMMAND = (
    "sidekick-usages",
    "migrate",
    "accounts",
)
_MIGRATE_LOCATIONS_COMMAND = (
    "sidekick-usages",
    "migrate",
    "locations",
)
_SCHEMA_READY_CODES = frozenset(
    {
        PersistenceCode.EMPTY,
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
    }
)
_OBSERVATION_BLOCKING_CODES = frozenset(
    {
        PersistenceCode.UNSUPPORTED_FILESYSTEM,
        PersistenceCode.UNSAFE_PERMISSIONS,
        PersistenceCode.UNREADABLE,
        PersistenceCode.DUPLICATE_KEY,
        PersistenceCode.MALFORMED_JSON,
        PersistenceCode.FUTURE_SCHEMA,
        PersistenceCode.INVALID_SCHEMA,
    }
)
_PARTIAL_CODES = frozenset(
    {
        PersistenceCode.BACKUP_CONFLICT,
        PersistenceCode.INTERRUPTED_ARTIFACTS,
        PersistenceCode.LEGACY_WRITER_DETECTED,
        PersistenceCode.SOURCE_CHANGED,
        PersistenceCode.REPLACE_FAILED,
        PersistenceCode.DURABILITY_UNCERTAIN,
        PersistenceCode.RESET_INCOMPLETE,
    }
)


class LocationRole(StrEnum):
    """Role of one persistence location in compatibility selection."""

    PROTOTYPE = "prototype"
    COMPATIBILITY = "compatibility"
    CANONICAL = "canonical"


class LocationCode(StrEnum):
    """Stable machine outcomes for persistence-location selection."""

    EMPTY = "empty"
    PROTOTYPE_ONLY = "prototype_only"
    COMPATIBILITY_SELECTED = "compatibility_selected"
    CANONICAL_SELECTED = "canonical_selected"
    EQUIVALENT_SELECTED = "equivalent_selected"
    CONFLICT = "conflict"
    PARTIAL = "partial"
    CANDIDATE_BLOCKED = "candidate_blocked"


@dataclass(frozen=True, slots=True)
class LocationCandidate:
    """One assessed location and its deterministic rewritten state."""

    role: LocationRole
    path: Path
    assessment: PersistenceAssessment
    account_digest: Sha256Digest | None
    private_auth_digest: Sha256Digest | None
    lineage_account_digests: frozenset[Sha256Digest] = frozenset()

    def __post_init__(self) -> None:
        _require_safe_absolute_path(self.path)
        if self.assessment.safe_path != self.path:
            raise ValueError(
                "Candidate path must match its schema assessment."
            )
        _validate_persistence_assessment(self.assessment)
        if any(
            not isinstance(digest, Sha256Digest)
            for digest in self.lineage_account_digests
        ):
            raise TypeError("Lineage account digests are invalid.")
        code = self.assessment.code
        if code is PersistenceCode.EMPTY:
            raise ValueError("An absent location is not a candidate.")
        if _is_blocked_candidate(self):
            if (
                self.account_digest is not None
                or self.private_auth_digest is not None
            ):
                raise ValueError(
                    "Blocked candidates cannot claim rewritten state."
                )
        elif (
            self.role is not LocationRole.PROTOTYPE
            and code not in _PARTIAL_CODES
            and self.account_digest is None
        ):
            raise ValueError(
                "Selectable candidates require rewritten account state."
            )


@dataclass(frozen=True, slots=True)
class EmptySelection:
    """No persistence candidate exists."""

    code: Literal[LocationCode.EMPTY] = field(
        default=LocationCode.EMPTY,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class PrototypeSelection:
    """Only the import-only prototype candidate exists."""

    candidate: LocationCandidate
    code: Literal[LocationCode.PROTOTYPE_ONLY] = field(
        default=LocationCode.PROTOTYPE_ONLY,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_role(self.candidate, LocationRole.PROTOTYPE)


@dataclass(frozen=True, slots=True)
class CompatibilitySelection:
    """The compatibility location is the runtime authority."""

    candidate: LocationCandidate
    code: Literal[LocationCode.COMPATIBILITY_SELECTED] = field(
        default=LocationCode.COMPATIBILITY_SELECTED,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_role(self.candidate, LocationRole.COMPATIBILITY)


@dataclass(frozen=True, slots=True)
class CanonicalSelection:
    """The canonical location is the runtime authority."""

    candidate: LocationCandidate
    code: Literal[LocationCode.CANONICAL_SELECTED] = field(
        default=LocationCode.CANONICAL_SELECTED,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_role(self.candidate, LocationRole.CANONICAL)


@dataclass(frozen=True, slots=True)
class EquivalentSelection:
    """Equivalent authorities select the canonical candidate."""

    candidate: LocationCandidate
    code: Literal[LocationCode.EQUIVALENT_SELECTED] = field(
        default=LocationCode.EQUIVALENT_SELECTED,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_role(self.candidate, LocationRole.CANONICAL)


@dataclass(frozen=True, slots=True)
class ConflictSelection:
    """Authoritative candidates have divergent rewritten accounts."""

    candidates: tuple[LocationCandidate, LocationCandidate]
    code: Literal[LocationCode.CONFLICT] = field(
        default=LocationCode.CONFLICT,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_authority_pair(self.candidates)


@dataclass(frozen=True, slots=True)
class PartialSelection:
    """Involved candidates contain incomplete or incoherent evidence."""

    candidates: tuple[LocationCandidate, ...]
    resumable_migration: bool = False
    code: Literal[LocationCode.PARTIAL] = field(
        default=LocationCode.PARTIAL,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("Partial selection requires involved candidates.")
        if type(self.resumable_migration) is not bool:
            raise TypeError("Resumable migration state must be Boolean.")
        _require_unique_candidates(self.candidates)


@dataclass(frozen=True, slots=True)
class CandidateBlockedSelection:
    """One exact candidate failure blocks authoritative selection."""

    candidate: LocationCandidate
    persistence_code: PersistenceCode
    code: Literal[LocationCode.CANDIDATE_BLOCKED] = field(
        default=LocationCode.CANDIDATE_BLOCKED,
        init=False,
    )

    def __post_init__(self) -> None:
        if (
            not _is_blocked_candidate(self.candidate)
            or self.persistence_code is not self.candidate.assessment.code
        ):
            raise ValueError(
                "Blocked selection requires the candidate's exact failure."
            )


type RuntimePersistenceSelection = (
    EmptySelection
    | PrototypeSelection
    | CompatibilitySelection
    | CanonicalSelection
    | EquivalentSelection
    | ConflictSelection
    | PartialSelection
    | CandidateBlockedSelection
)
type ReadyLocationSelection = (
    EmptySelection
    | CompatibilitySelection
    | CanonicalSelection
    | EquivalentSelection
)
type BlockedLocationSelection = (
    PrototypeSelection
    | ConflictSelection
    | PartialSelection
    | CandidateBlockedSelection
)


def is_ready_location_selection(
    selection: RuntimePersistenceSelection,
) -> TypeIs[ReadyLocationSelection]:
    """Return whether a selection authorizes normal runtime access."""
    return isinstance(
        selection,
        (
            EmptySelection,
            CompatibilitySelection,
            CanonicalSelection,
            EquivalentSelection,
        ),
    )


def is_blocked_location_selection(
    selection: RuntimePersistenceSelection,
) -> TypeIs[BlockedLocationSelection]:
    """Return whether a selection requires an explicit recovery action."""
    return isinstance(
        selection,
        (
            PrototypeSelection,
            ConflictSelection,
            PartialSelection,
            CandidateBlockedSelection,
        ),
    )


def ready_location_assessment(
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> LocationMigrationAssessment[ReadyLocationSelection]:
    """Narrow one validated assessment to its runtime-ready form."""
    selection = assessment.selection
    if not is_ready_location_selection(selection):
        raise ValueError("Location assessment is not runtime-ready.")
    return LocationMigrationAssessment(
        selection=selection,
        candidates=assessment.candidates,
        source=assessment.source,
        destination=assessment.destination,
        private_auth_summary=assessment.private_auth_summary,
        artifact_basename=assessment.artifact_basename,
        issues=assessment.issues,
        write_blocked=assessment.write_blocked,
        next_command=assessment.next_command,
    )


def blocked_location_assessment(
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> LocationMigrationAssessment[BlockedLocationSelection]:
    """Narrow one validated assessment to its explicit blocked form."""
    selection = assessment.selection
    if not is_blocked_location_selection(selection):
        raise ValueError("Location assessment is not blocked.")
    return LocationMigrationAssessment(
        selection=selection,
        candidates=assessment.candidates,
        source=assessment.source,
        destination=assessment.destination,
        private_auth_summary=assessment.private_auth_summary,
        artifact_basename=assessment.artifact_basename,
        issues=assessment.issues,
        write_blocked=assessment.write_blocked,
        next_command=assessment.next_command,
    )


def location_persistence_code(
    selection: RuntimePersistenceSelection,
) -> PersistenceCode:
    """Map a location selection to its exact persistence exit vocabulary."""
    if isinstance(selection, EmptySelection):
        return PersistenceCode.EMPTY
    if isinstance(selection, CandidateBlockedSelection):
        return selection.persistence_code
    if isinstance(selection, PrototypeSelection):
        return PersistenceCode.PROTOTYPE_IMPORT_REQUIRED
    if isinstance(selection, (ConflictSelection, PartialSelection)):
        return PersistenceCode.BACKUP_CONFLICT
    return selection.candidate.assessment.code


@dataclass(frozen=True, slots=True)
class LocationMigrationAssessment[S: RuntimePersistenceSelection]:
    """Complete deterministic location state for one invocation."""

    selection: S
    candidates: tuple[LocationCandidate, ...]
    source: Path
    destination: Path
    private_auth_summary: (
        PrivateAuthMigrationAssessment | PrivateAuthMigrationFailure
    )
    artifact_basename: str | None
    issues: tuple[PersistenceIssue, ...]
    write_blocked: bool
    next_command: tuple[str, ...] | None

    def __post_init__(self) -> None:
        _validate_assessment_candidates(
            self.selection,
            self.candidates,
            self.source,
            self.destination,
        )
        if not isinstance(
            self.private_auth_summary,
            (PrivateAuthMigrationAssessment, PrivateAuthMigrationFailure),
        ):
            raise TypeError(
                "Private-auth summary must be a migration assessment."
            )
        _validate_assessment_output(
            self.artifact_basename,
            self.issues,
        )
        _validate_assessment_action(
            self.selection,
            self.write_blocked,
            self.next_command,
        )


@dataclass(frozen=True, slots=True)
class LocationMigrationPlan:
    """One validated compatibility-to-canonical relocation plan."""

    assessment: LocationMigrationAssessment[CompatibilitySelection]

    def __post_init__(self) -> None:
        if not isinstance(
            self.assessment.selection,
            CompatibilitySelection,
        ):
            raise ValueError(
                "Location migration requires compatibility authority."
            )
        if self.assessment.source == self.assessment.destination:
            raise ValueError("Location migration source must be distinct.")


@dataclass(frozen=True, slots=True)
class LocationMigrationResult:
    """A relocation plan plus its fresh canonical-ready assessment."""

    plan: LocationMigrationPlan
    assessment: (
        LocationMigrationAssessment[CanonicalSelection]
        | LocationMigrationAssessment[EquivalentSelection]
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.assessment.selection,
            (CanonicalSelection, EquivalentSelection),
        ):
            raise ValueError(
                "Location migration result must select canonical authority."
            )
        if self.plan.assessment.destination != self.assessment.destination:
            raise ValueError("Migration result destination changed.")


def location_migration_plan(
    candidate: LocationCandidate,
    private_auth: PrivateAuthMigrationAssessment | PrivateAuthMigrationFailure,
    destination: Path,
) -> LocationMigrationPlan:
    """Build the exact compatibility-to-canonical operation plan."""
    selection = CompatibilitySelection(candidate)
    return LocationMigrationPlan(
        LocationMigrationAssessment(
            selection=selection,
            candidates=(candidate,),
            source=candidate.path,
            destination=destination,
            private_auth_summary=private_auth,
            artifact_basename=candidate.assessment.artifact_basename,
            issues=candidate.assessment.issues,
            write_blocked=False,
            next_command=_MIGRATE_LOCATIONS_COMMAND,
        )
    )


def completed_location_assessment[
    S: CanonicalSelection | EquivalentSelection,
](
    selection: S,
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> LocationMigrationAssessment[S]:
    """Narrow one completed canonical location assessment."""
    return LocationMigrationAssessment(
        selection=selection,
        candidates=assessment.candidates,
        source=assessment.source,
        destination=assessment.destination,
        private_auth_summary=assessment.private_auth_summary,
        artifact_basename=assessment.artifact_basename,
        issues=assessment.issues,
        write_blocked=assessment.write_blocked,
        next_command=assessment.next_command,
    )


def select_runtime_persistence(
    candidates: tuple[LocationCandidate, ...],
) -> RuntimePersistenceSelection:
    """Select runtime authority from complete passive location evidence."""
    _require_unique_candidates(candidates)
    by_role = {candidate.role: candidate for candidate in candidates}
    prototype = by_role.get(LocationRole.PROTOTYPE)
    authorities = tuple(
        candidate
        for role in (LocationRole.COMPATIBILITY, LocationRole.CANONICAL)
        if (candidate := by_role.get(role)) is not None
    )
    if authorities:
        return _select_authorities(authorities)
    return _select_prototype(prototype)


def _select_authorities(
    authorities: tuple[LocationCandidate, ...],
) -> (
    ReadyLocationSelection
    | ConflictSelection
    | PartialSelection
    | (CandidateBlockedSelection)
):
    blocking = tuple(
        candidate
        for candidate in authorities
        if _is_blocked_candidate(candidate)
    )
    if blocking:
        return _blocked_selection(blocking)
    if any(
        candidate.assessment.code in _PARTIAL_CODES
        for candidate in authorities
    ):
        return PartialSelection(authorities)
    if any(
        candidate.account_digest is None
        or candidate.private_auth_digest is None
        for candidate in authorities
    ):
        return PartialSelection(authorities)
    if len(authorities) == 1:
        candidate = authorities[0]
        if candidate.role is LocationRole.COMPATIBILITY:
            return CompatibilitySelection(candidate)
        return CanonicalSelection(candidate)
    return _select_authority_pair(authorities)


def _blocked_selection(
    candidates: tuple[LocationCandidate, ...],
) -> CandidateBlockedSelection:
    candidate = min(
        candidates,
        key=lambda item: (
            passive_priority(item.assessment.code),
            _role_rank(item.role),
        ),
    )
    return CandidateBlockedSelection(
        candidate,
        candidate.assessment.code,
    )


def _select_prototype(
    prototype: LocationCandidate | None,
) -> (
    EmptySelection
    | PrototypeSelection
    | PartialSelection
    | (CandidateBlockedSelection)
):
    if prototype is None:
        return EmptySelection()
    if _is_blocked_candidate(prototype):
        return CandidateBlockedSelection(
            prototype,
            prototype.assessment.code,
        )
    if prototype.assessment.code in _PARTIAL_CODES:
        return PartialSelection((prototype,))
    return PrototypeSelection(prototype)


def _select_authority_pair(
    authorities: tuple[LocationCandidate, ...],
) -> (
    CanonicalSelection
    | EquivalentSelection
    | ConflictSelection
    | PartialSelection
):
    compatibility, canonical = authorities
    pair = (compatibility, canonical)
    if compatibility.account_digest != canonical.account_digest:
        if compatibility.account_digest in canonical.lineage_account_digests:
            return CanonicalSelection(canonical)
        return ConflictSelection(pair)
    if compatibility.private_auth_digest != canonical.private_auth_digest:
        return PartialSelection(pair)
    return EquivalentSelection(canonical)


def _require_role(
    candidate: LocationCandidate,
    expected: LocationRole,
) -> None:
    if candidate.role is not expected:
        raise ValueError(f"Selection requires the {expected.value} role.")


def _require_authority_pair(
    candidates: tuple[LocationCandidate, LocationCandidate],
) -> None:
    _require_unique_candidates(candidates)
    if tuple(candidate.role for candidate in candidates) != (
        LocationRole.COMPATIBILITY,
        LocationRole.CANONICAL,
    ):
        raise ValueError(
            "Conflicting candidates require compatibility then canonical."
        )


def _require_unique_candidates(
    candidates: tuple[LocationCandidate, ...],
) -> None:
    roles = tuple(candidate.role for candidate in candidates)
    paths = tuple(candidate.path for candidate in candidates)
    if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
        raise ValueError("Location candidates require unique roles and paths.")


def _selection_candidates(
    selection: RuntimePersistenceSelection,
) -> tuple[LocationCandidate, ...]:
    if isinstance(selection, EmptySelection):
        return ()
    if isinstance(selection, (ConflictSelection, PartialSelection)):
        return selection.candidates
    return (selection.candidate,)


def _expected_next_command(
    selection: RuntimePersistenceSelection,
) -> tuple[str, ...] | None:
    if isinstance(selection, PrototypeSelection):
        return _MIGRATE_ACCOUNTS_COMMAND
    if isinstance(selection, CompatibilitySelection):
        return _MIGRATE_LOCATIONS_COMMAND
    if isinstance(selection, PartialSelection) and (
        selection.resumable_migration
    ):
        return _MIGRATE_LOCATIONS_COMMAND
    if isinstance(selection, CandidateBlockedSelection):
        return selection.candidate.assessment.next_command
    return None


def _validate_assessment_candidates(
    selection: RuntimePersistenceSelection,
    candidates: tuple[LocationCandidate, ...],
    source: Path,
    destination: Path,
) -> None:
    _require_safe_absolute_path(source)
    _require_safe_absolute_path(destination)
    _require_unique_candidates(candidates)
    involved = _selection_candidates(selection)
    if any(candidate not in candidates for candidate in involved):
        raise ValueError(
            "Selection candidates must occur in observed candidates."
        )
    if involved and source not in {candidate.path for candidate in involved}:
        raise ValueError("Assessment source must be an involved candidate.")
    canonical = next(
        (
            candidate
            for candidate in candidates
            if candidate.role is LocationRole.CANONICAL
        ),
        None,
    )
    if canonical is not None and canonical.path != destination:
        raise ValueError(
            "Canonical candidate must match the migration destination."
        )


def _validate_assessment_output(
    artifact_basename: str | None,
    issues: tuple[PersistenceIssue, ...],
) -> None:
    if artifact_basename is not None:
        require_safe_observation_basename(artifact_basename)
    for issue in issues:
        if issue.artifact_basename is not None:
            require_safe_observation_basename(issue.artifact_basename)


def _validate_assessment_action(
    selection: RuntimePersistenceSelection,
    write_blocked: bool,
    next_command: tuple[str, ...] | None,
) -> None:
    expected_blocked = isinstance(
        selection,
        (
            PrototypeSelection,
            ConflictSelection,
            PartialSelection,
            CandidateBlockedSelection,
        ),
    )
    if type(write_blocked) is not bool:
        raise TypeError("Location write-blocked state must be Boolean.")
    if write_blocked is not expected_blocked:
        raise ValueError("Location write-blocked state is contradictory.")
    _require_safe_command(next_command)
    if next_command != _expected_next_command(selection):
        raise ValueError("Location next command is contradictory.")


def _validate_persistence_assessment(
    assessment: PersistenceAssessment,
) -> None:
    _require_safe_absolute_path(assessment.safe_path)
    if assessment.artifact_basename is not None:
        require_safe_observation_basename(assessment.artifact_basename)
    if type(assessment.write_blocked) is not bool:
        raise TypeError("Schema write-blocked state must be Boolean.")
    expected_blocked = assessment.code not in _SCHEMA_READY_CODES
    if assessment.write_blocked is not expected_blocked:
        raise ValueError("Schema write-blocked state is contradictory.")
    _require_safe_command(assessment.next_command)
    for issue in assessment.issues:
        if issue.artifact_basename is not None:
            require_safe_observation_basename(issue.artifact_basename)


def _require_safe_absolute_path(path: Path) -> None:
    text = str(path)
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or ".." in path.parts
        or "\x00" in text
    ):
        raise ValueError("Location paths must be safe absolute file paths.")
    require_safe_observation_basename(path.name)


def _require_safe_command(command: tuple[str, ...] | None) -> None:
    if command is None:
        return
    if not command or any(
        not part or any(unicodedata.category(char) == "Cc" for char in part)
        for part in command
    ):
        raise ValueError("Recovery command must contain safe arguments.")


def _is_blocked_candidate(candidate: LocationCandidate) -> bool:
    code = candidate.assessment.code
    if candidate.role is LocationRole.PROTOTYPE:
        return code in _OBSERVATION_BLOCKING_CODES
    return (
        code not in _SCHEMA_READY_CODES
        and code not in _PARTIAL_CODES
        and code is not PersistenceCode.EMPTY
    )


def _role_rank(role: LocationRole) -> int:
    return {
        LocationRole.COMPATIBILITY: 0,
        LocationRole.CANONICAL: 1,
        LocationRole.PROTOTYPE: 2,
    }[role]


__all__ = [
    "BlockedLocationSelection",
    "CandidateBlockedSelection",
    "CanonicalSelection",
    "CompatibilitySelection",
    "ConflictSelection",
    "EmptySelection",
    "EquivalentSelection",
    "LocationCandidate",
    "LocationCode",
    "LocationMigrationAssessment",
    "LocationMigrationPlan",
    "LocationMigrationResult",
    "LocationRole",
    "PartialSelection",
    "PrototypeSelection",
    "ReadyLocationSelection",
    "RuntimePersistenceSelection",
    "blocked_location_assessment",
    "completed_location_assessment",
    "is_blocked_location_selection",
    "is_ready_location_selection",
    "location_migration_plan",
    "location_persistence_code",
    "ready_location_assessment",
    "select_runtime_persistence",
]
