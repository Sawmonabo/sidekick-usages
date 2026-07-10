"""Behavioral contracts for pure persistence-location selection."""

from pathlib import Path

import pytest

from sidekick_usages.persistence.artifacts import Sha256Digest
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.migrations.location import (
    CandidateBlockedSelection,
    CanonicalSelection,
    CompatibilitySelection,
    ConflictSelection,
    EmptySelection,
    EquivalentSelection,
    LocationCandidate,
    LocationCode,
    LocationMigrationAssessment,
    LocationMigrationPlan,
    LocationMigrationResult,
    LocationRole,
    PartialSelection,
    PrototypeSelection,
    RuntimePersistenceSelection,
    select_runtime_persistence,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationAssessment,
)
from sidekick_usages.persistence.observations import StoredGeneration

COMPATIBILITY_PATH = Path("/synthetic/compatibility/accounts.json")
CANONICAL_PATH = Path("/synthetic/canonical/accounts.json")
PROTOTYPE_PATH = Path("/synthetic/prototype/accounts.json")
MIGRATE_ACCOUNTS = ("sidekick-usages", "migrate", "accounts")
MIGRATE_LOCATIONS = ("sidekick-usages", "migrate", "locations")
REPAIR_PERMISSIONS = ("sidekick-usages", "permissions", "repair")
BLOCKING_CODES = (
    PersistenceCode.UNSAFE_PERMISSIONS,
    PersistenceCode.MALFORMED_JSON,
    PersistenceCode.UNREADABLE,
    PersistenceCode.FUTURE_SCHEMA,
)


def _assessment(path: Path, code: PersistenceCode) -> PersistenceAssessment:
    ready = code in {
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
    }
    if code is PersistenceCode.PROTOTYPE_IMPORT_REQUIRED:
        command = MIGRATE_ACCOUNTS
        generation = StoredGeneration.ABSENT
    elif code is PersistenceCode.MIGRATION_REQUIRED:
        command = MIGRATE_ACCOUNTS
        generation = StoredGeneration.GENERATION_ZERO
    elif code is PersistenceCode.ROLLBACK_PREPARED:
        command = None
        generation = StoredGeneration.GENERATION_ZERO
    elif code is PersistenceCode.UNSAFE_PERMISSIONS:
        command = REPAIR_PERMISSIONS
        generation = StoredGeneration.UNKNOWN
    else:
        command = None
        generation = (
            StoredGeneration.FUTURE
            if code is PersistenceCode.FUTURE_SCHEMA
            else StoredGeneration.VERSION_ONE
        )
    return PersistenceAssessment(
        code=code,
        generation=generation,
        schema_version=2 if code is PersistenceCode.FUTURE_SCHEMA else 1,
        account_count=None if code in BLOCKING_CODES else 1,
        safe_path=path,
        artifact_basename=None,
        write_blocked=not ready,
        next_command=command,
        message=f"Synthetic {code.value} evidence.",
        issues=(PersistenceIssue(code, None, "Synthetic issue."),),
    )


def _candidate(
    role: LocationRole,
    *,
    code: PersistenceCode | None = None,
    account: str = "a",
    private_auth: str | None = "b",
) -> LocationCandidate:
    path = {
        LocationRole.PROTOTYPE: PROTOTYPE_PATH,
        LocationRole.COMPATIBILITY: COMPATIBILITY_PATH,
        LocationRole.CANONICAL: CANONICAL_PATH,
    }[role]
    resolved_code = code or (
        PersistenceCode.PROTOTYPE_IMPORT_REQUIRED
        if role is LocationRole.PROTOTYPE
        else PersistenceCode.CURRENT
    )
    blocked = resolved_code in {
        *BLOCKING_CODES,
        PersistenceCode.MIGRATION_REQUIRED,
        PersistenceCode.ROLLBACK_PREPARED,
    }
    return LocationCandidate(
        role=role,
        path=path,
        assessment=_assessment(path, resolved_code),
        account_digest=None if blocked else Sha256Digest(account * 64),
        private_auth_digest=(
            None
            if blocked or private_auth is None
            else Sha256Digest(private_auth * 64)
        ),
    )


COMPATIBILITY = _candidate(LocationRole.COMPATIBILITY)
CANONICAL = _candidate(LocationRole.CANONICAL)
PROTOTYPE = _candidate(LocationRole.PROTOTYPE)


def test_location_code_is_the_exact_stable_machine_vocabulary() -> None:
    """Location JSON cannot drift into provisional schema-state codes."""
    assert tuple(code.value for code in LocationCode) == (
        "empty",
        "prototype_only",
        "compatibility_selected",
        "canonical_selected",
        "equivalent_selected",
        "conflict",
        "partial",
        "candidate_blocked",
    )


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        pytest.param((), EmptySelection(), id="empty"),
        pytest.param(
            (COMPATIBILITY,),
            CompatibilitySelection(COMPATIBILITY),
            id="compatibility-only",
        ),
        pytest.param(
            (CANONICAL,),
            CanonicalSelection(CANONICAL),
            id="canonical-only",
        ),
        pytest.param(
            (COMPATIBILITY, CANONICAL),
            EquivalentSelection(CANONICAL),
            id="semantically-equivalent",
        ),
        pytest.param(
            (
                COMPATIBILITY,
                _candidate(LocationRole.CANONICAL, account="c"),
            ),
            ConflictSelection(
                (
                    COMPATIBILITY,
                    _candidate(LocationRole.CANONICAL, account="c"),
                )
            ),
            id="account-conflict",
        ),
        pytest.param(
            (
                COMPATIBILITY,
                _candidate(LocationRole.CANONICAL, private_auth="c"),
            ),
            PartialSelection(
                (
                    COMPATIBILITY,
                    _candidate(LocationRole.CANONICAL, private_auth="c"),
                )
            ),
            id="private-auth-incoherent",
        ),
        pytest.param(
            (
                _candidate(
                    LocationRole.COMPATIBILITY,
                    code=PersistenceCode.INTERRUPTED_ARTIFACTS,
                ),
            ),
            PartialSelection(
                (
                    _candidate(
                        LocationRole.COMPATIBILITY,
                        code=PersistenceCode.INTERRUPTED_ARTIFACTS,
                    ),
                )
            ),
            id="interrupted-evidence",
        ),
        pytest.param(
            (
                _candidate(
                    LocationRole.COMPATIBILITY,
                    private_auth=None,
                ),
            ),
            PartialSelection(
                (
                    _candidate(
                        LocationRole.COMPATIBILITY,
                        private_auth=None,
                    ),
                )
            ),
            id="private-auth-assessment-failed",
        ),
        pytest.param(
            (PROTOTYPE,),
            PrototypeSelection(PROTOTYPE),
            id="prototype-only",
        ),
    ],
)
def test_generation_matrix_selects_one_closed_variant(
    candidates: tuple[LocationCandidate, ...],
    expected: RuntimePersistenceSelection,
) -> None:
    """Runtime authority follows semantic state, not candidate byte order."""
    assert select_runtime_persistence(candidates) == expected


@pytest.mark.parametrize("code", BLOCKING_CODES)
def test_candidate_failures_stay_exact_while_stale_prototype_is_ignored(
    code: PersistenceCode,
) -> None:
    """Authority failures fail closed without reviving prototype fallback."""
    blocked = _candidate(LocationRole.COMPATIBILITY, code=code)
    stale_prototype = _candidate(LocationRole.PROTOTYPE, code=code)

    assert select_runtime_persistence((blocked,)) == (
        CandidateBlockedSelection(blocked, code)
    )
    assert select_runtime_persistence((CANONICAL, stale_prototype)) == (
        CanonicalSelection(CANONICAL)
    )


@pytest.mark.parametrize(
    "code",
    [PersistenceCode.MIGRATION_REQUIRED, PersistenceCode.ROLLBACK_PREPARED],
)
def test_generation_zero_never_becomes_runtime_ready(
    code: PersistenceCode,
) -> None:
    """Valid legacy bytes retain their exact schema migration gate."""
    candidate = _candidate(LocationRole.COMPATIBILITY, code=code)

    assert select_runtime_persistence((candidate,)) == (
        CandidateBlockedSelection(candidate, code)
    )


def test_assessment_plan_and_result_reject_contradictory_state() -> None:
    """Validated contracts cannot authorize an unsafe relocation lifecycle."""
    before = LocationMigrationAssessment(
        selection=CompatibilitySelection(COMPATIBILITY),
        candidates=(COMPATIBILITY,),
        source=COMPATIBILITY_PATH,
        destination=CANONICAL_PATH,
        private_auth_summary=PrivateAuthMigrationAssessment(()),
        artifact_basename=None,
        issues=COMPATIBILITY.assessment.issues,
        write_blocked=False,
        next_command=MIGRATE_LOCATIONS,
    )
    plan = LocationMigrationPlan(before)
    after = LocationMigrationAssessment(
        selection=EquivalentSelection(CANONICAL),
        candidates=(COMPATIBILITY, CANONICAL),
        source=CANONICAL_PATH,
        destination=CANONICAL_PATH,
        private_auth_summary=PrivateAuthMigrationAssessment(()),
        artifact_basename=None,
        issues=(),
        write_blocked=False,
        next_command=None,
    )
    result = LocationMigrationResult(plan, after)

    assert result.assessment.selection == EquivalentSelection(CANONICAL)
    with pytest.raises(ValueError, match="write-blocked"):
        LocationMigrationAssessment(
            selection=CompatibilitySelection(COMPATIBILITY),
            candidates=(COMPATIBILITY,),
            source=COMPATIBILITY_PATH,
            destination=CANONICAL_PATH,
            private_auth_summary=PrivateAuthMigrationAssessment(()),
            artifact_basename=None,
            issues=(),
            write_blocked=True,
            next_command=MIGRATE_LOCATIONS,
        )
    relative = Path("relative/accounts.json")
    with pytest.raises(ValueError, match="safe absolute"):
        LocationCandidate(
            role=LocationRole.CANONICAL,
            path=relative,
            assessment=_assessment(relative, PersistenceCode.CURRENT),
            account_digest=Sha256Digest("a" * 64),
            private_auth_digest=Sha256Digest("b" * 64),
        )
