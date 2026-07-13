"""Typed state and role selection for location migration work."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.paths import AccountLocations
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.migrations.account import MigrationTransaction
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
)
from sidekick_usages.persistence.migrations.location import (
    CandidateBlockedSelection,
    CanonicalSelection,
    CompatibilitySelection,
    EmptySelection,
    EquivalentSelection,
    LocationMigrationAssessment,
    LocationMigrationPlan,
    LocationRole,
    PrototypeSelection,
    ReadyLocationSelection,
    RuntimePersistenceSelection,
    is_ready_location_selection,
)
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)


@dataclass(frozen=True, slots=True)
class RuntimePersistence:
    """One ready runtime authority and its private credential boundary."""

    locations: AccountLocations
    private_credentials: PrivateCredentialTree = field(repr=False)
    assessment: LocationMigrationAssessment[ReadyLocationSelection]


@dataclass(frozen=True, slots=True)
class HeldLocationState:
    """Both held authorities and their active transactions."""

    compatibility_filesystem: PersistenceFilesystem = field(repr=False)
    canonical_filesystem: PersistenceFilesystem = field(repr=False)
    compatibility_transaction: MigrationTransaction = field(repr=False)
    canonical_transaction: MigrationTransaction = field(repr=False)


@dataclass(frozen=True, slots=True)
class LocationMigrationWork:
    """Prepared canonical publication and private-auth migration."""

    plan: LocationMigrationPlan
    source: FileSnapshot = field(repr=False)
    target: FileSnapshot | None = field(repr=False)
    private_auth: PreparedPrivateAuthMigration = field(repr=False)
    source_accounts: tuple[Account, ...] = field(repr=False)
    payload: bytes = field(repr=False)
    displaced_bundles: tuple[Path, ...] = field(repr=False)

    @property
    def expected_target(self) -> ExpectedAuthority:
        """Return the exact canonical base expectation."""
        if self.target is None:
            return AuthorityExpectation.ABSENT
        return self.target.fingerprint

    @property
    def base_generation(self) -> AuthorityGeneration | None:
        """Return the canonical base generation when present."""
        if self.target is None:
            return None
        return AuthorityGeneration.VERSION_TWO


@dataclass(frozen=True, slots=True)
class RollbackTarget:
    """Prepared version-two rollback publication target."""

    snapshot: FileSnapshot | None = field(repr=False)
    expected: ExpectedAuthority
    base_generation: AuthorityGeneration | None


def path_text(path: Path) -> str:
    """Return a deterministic path sort key."""
    return str(path)


def ready_role(selection: ReadyLocationSelection) -> LocationRole:
    """Return the selected authority role for ready runtime state."""
    if isinstance(selection, EmptySelection):
        return LocationRole.CANONICAL
    if isinstance(selection, CompatibilitySelection):
        return LocationRole.COMPATIBILITY
    if isinstance(selection, (CanonicalSelection, EquivalentSelection)):
        return LocationRole.CANONICAL
    raise TypeError("Unknown ready location selection.")


def operation_role(
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> LocationRole:
    """Return the authority role an explicit operation may mutate."""
    selection = assessment.selection
    if is_ready_location_selection(selection):
        return ready_role(selection)
    if isinstance(selection, PrototypeSelection):
        return LocationRole.CANONICAL
    if isinstance(selection, CandidateBlockedSelection):
        if selection.candidate.role is LocationRole.PROTOTYPE:
            return LocationRole.CANONICAL
        return selection.candidate.role
    raise LocationMigrationStateError(assessment)


__all__ = [
    "HeldLocationState",
    "LocationMigrationWork",
    "RollbackTarget",
    "RuntimePersistence",
    "operation_role",
    "path_text",
    "ready_role",
]
