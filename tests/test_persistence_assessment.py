"""Deterministic passive persistence assessment tests."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest

from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.assessment import (
    ArtifactKind,
    ArtifactObservation,
    ArtifactState,
    AuthorityKind,
    AuthorityObservation,
    PersistenceCode,
    PersistenceObservation,
    StoredGeneration,
    assess_persistence,
    doctor_exit_code,
    make_operation_result,
    operation_exit_code,
    passive_priority,
)
from sidekick_usages.persistence.migrations import (
    prototype_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeReceipt,
    VersionOneDocument,
    decode_prototype,
    encode_generation_zero,
    encode_version_one,
)

SAFE_PATH = Path("/synthetic/sidekick/accounts.json")
EMPTY_G0 = GenerationZeroDocument(())
EMPTY_V1 = VersionOneDocument(())
PROTOTYPE_BYTES = b"""{
  "primary": {
    "token": "prototype-secret",
    "plan": "max"
  }
}
"""
PROTOTYPE = decode_prototype(PROTOTYPE_BYTES)
PROTOTYPE_V1 = prototype_to_version_one(PROTOTYPE)
PROTOTYPE_G0 = version_one_to_v060(PROTOTYPE_V1)
PROTOTYPE_RECEIPT = PrototypeReceipt(
    sha256(PROTOTYPE_BYTES).hexdigest(),
)
HISTORICAL_RECEIPT = PrototypeReceipt("0" * 64)
MIGRATE = ("sidekick-usages", "migrate", "accounts")


def _absent() -> AuthorityObservation:
    return AuthorityObservation(AuthorityKind.ABSENT)


def _generation_zero(
    document: GenerationZeroDocument = EMPTY_G0,
) -> AuthorityObservation:
    return AuthorityObservation(
        AuthorityKind.GENERATION_ZERO,
        content=encode_generation_zero(document),
        generation_zero=document,
    )


def _version_one(
    document: VersionOneDocument = EMPTY_V1,
) -> AuthorityObservation:
    return AuthorityObservation(
        AuthorityKind.VERSION_ONE,
        content=encode_version_one(document),
        version_one=document,
    )


def _simple_artifact(
    kind: ArtifactKind,
    state: ArtifactState,
    basename: str,
) -> ArtifactObservation:
    return ArtifactObservation(kind, basename, state)


def _v0_backup(
    document: GenerationZeroDocument = EMPTY_G0,
    basename: str = "accounts.v0.backup",
) -> ArtifactObservation:
    return ArtifactObservation(
        ArtifactKind.V0_BACKUP,
        basename,
        ArtifactState.VALID,
        content=encode_generation_zero(document),
        generation_zero=document,
    )


def _v1_snapshot(
    document: VersionOneDocument = EMPTY_V1,
    basename: str = "accounts.v1.snapshot",
) -> ArtifactObservation:
    return ArtifactObservation(
        ArtifactKind.V1_SNAPSHOT,
        basename,
        ArtifactState.VALID,
        content=encode_version_one(document),
        version_one=document,
    )


def _prototype(
    state: ArtifactState = ArtifactState.VALID,
) -> ArtifactObservation:
    readable = state not in {
        ArtifactState.UNSAFE,
        ArtifactState.UNREADABLE,
    }
    return ArtifactObservation(
        ArtifactKind.PROTOTYPE,
        "accounts.prototype.json",
        state,
        content=PROTOTYPE_BYTES if readable else None,
        prototype=PROTOTYPE if state is ArtifactState.VALID else None,
    )


def _receipt(
    receipt: PrototypeReceipt = PROTOTYPE_RECEIPT,
    *,
    basename: str = "accounts.prototype.receipt.json",
) -> ArtifactObservation:
    return ArtifactObservation(
        ArtifactKind.PROTOTYPE_RECEIPT,
        basename,
        ArtifactState.VALID,
        receipt=receipt,
    )


def _observe(
    authority: AuthorityObservation,
    *artifacts: ArtifactObservation,
    orphaned_credentials: bool = False,
) -> PersistenceObservation:
    return PersistenceObservation(
        SAFE_PATH,
        authority,
        artifacts,
        orphaned_credentials,
    )


@pytest.mark.parametrize(
    ("observation", "code", "generation", "schema_version", "account_count"),
    [
        pytest.param(
            _observe(_absent()),
            PersistenceCode.EMPTY,
            StoredGeneration.ABSENT,
            None,
            0,
            id="empty",
        ),
        pytest.param(
            _observe(_generation_zero()),
            PersistenceCode.MIGRATION_REQUIRED,
            StoredGeneration.GENERATION_ZERO,
            None,
            0,
            id="generation-zero",
        ),
        pytest.param(
            _observe(_version_one()),
            PersistenceCode.CURRENT,
            StoredGeneration.VERSION_ONE,
            1,
            0,
            id="version-one-first-write",
        ),
        pytest.param(
            _observe(
                AuthorityObservation(
                    AuthorityKind.FUTURE,
                    future_schema_version=2,
                )
            ),
            PersistenceCode.FUTURE_SCHEMA,
            StoredGeneration.FUTURE,
            2,
            None,
            id="future",
        ),
        *[
            pytest.param(
                _observe(AuthorityObservation(kind)),
                code,
                StoredGeneration.UNKNOWN,
                None,
                None,
                id=kind.value,
            )
            for kind, code in (
                (
                    AuthorityKind.UNSUPPORTED_FILESYSTEM,
                    PersistenceCode.UNSUPPORTED_FILESYSTEM,
                ),
                (
                    AuthorityKind.UNSAFE,
                    PersistenceCode.UNSAFE_PERMISSIONS,
                ),
                (AuthorityKind.UNREADABLE, PersistenceCode.UNREADABLE),
                (AuthorityKind.DUPLICATE_KEY, PersistenceCode.DUPLICATE_KEY),
                (AuthorityKind.MALFORMED_JSON, PersistenceCode.MALFORMED_JSON),
                (AuthorityKind.INVALID_SCHEMA, PersistenceCode.INVALID_SCHEMA),
            )
        ],
        pytest.param(
            _observe(_absent(), _v0_backup()),
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            StoredGeneration.ABSENT,
            None,
            None,
            id="credentials-without-authority",
        ),
        pytest.param(
            _observe(_absent(), orphaned_credentials=True),
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            StoredGeneration.ABSENT,
            None,
            None,
            id="orphaned-private-credentials",
        ),
        pytest.param(
            _observe(
                _generation_zero(),
                _simple_artifact(
                    ArtifactKind.TEMPORARY,
                    ArtifactState.VALID,
                    "accounts.output.tmp",
                ),
            ),
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            StoredGeneration.GENERATION_ZERO,
            None,
            0,
            id="generation-zero-temporary",
        ),
        pytest.param(
            _observe(
                _version_one(),
                _simple_artifact(
                    ArtifactKind.TEMPORARY,
                    ArtifactState.VALID,
                    "accounts.output.tmp",
                ),
            ),
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            StoredGeneration.VERSION_ONE,
            1,
            0,
            id="version-one-temporary",
        ),
    ],
)
def test_authority_reduction_uses_only_restart_derivable_evidence(
    observation: PersistenceObservation,
    code: PersistenceCode,
    generation: StoredGeneration,
    schema_version: int | None,
    account_count: int | None,
) -> None:
    """Every authority class reduces to its documented passive state."""
    assessment = assess_persistence(observation)

    assert (
        assessment.code,
        assessment.generation,
        assessment.schema_version,
        assessment.account_count,
    ) == (code, generation, schema_version, account_count)
    assert assessment.issues[0].code is code
    assert assessment.safe_path == SAFE_PATH


@pytest.mark.parametrize(
    ("observation", "code"),
    [
        pytest.param(
            _observe(_generation_zero(), _v1_snapshot()),
            PersistenceCode.ROLLBACK_PREPARED,
            id="exact-v1-reverse",
        ),
        pytest.param(
            _observe(_generation_zero(), _v1_snapshot(PROTOTYPE_V1)),
            PersistenceCode.LEGACY_WRITER_DETECTED,
            id="nonmatching-v1-history",
        ),
        pytest.param(
            _observe(
                _generation_zero(),
                _v0_backup(),
                _v0_backup(PROTOTYPE_G0, "older.v0.backup"),
            ),
            PersistenceCode.MIGRATION_REQUIRED,
            id="multiple-v0-history",
        ),
        pytest.param(
            _observe(_version_one(), _v1_snapshot()),
            PersistenceCode.CURRENT,
            id="v1-snapshot-beside-v1",
        ),
    ],
)
def test_backup_relations_distinguish_history_from_rollback_proof(
    observation: PersistenceObservation,
    code: PersistenceCode,
) -> None:
    """Only an exact reversible v1 snapshot changes logical generation zero."""
    assert assess_persistence(observation).code is code


@pytest.mark.parametrize(
    ("authority", "artifacts", "code", "count", "command"),
    [
        pytest.param(
            _absent(),
            (_prototype(),),
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            1,
            MIGRATE,
            id="new-import",
        ),
        pytest.param(
            _absent(),
            (_prototype(), _receipt(HISTORICAL_RECEIPT)),
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            1,
            (*MIGRATE, "--reimport-prototype"),
            id="changed-prototype",
        ),
        pytest.param(
            _absent(),
            (_prototype(), _receipt()),
            PersistenceCode.EMPTY,
            0,
            None,
            id="matching-receipt-suppresses-import",
        ),
        pytest.param(
            _absent(),
            (_prototype(ArtifactState.MALFORMED_JSON), _receipt()),
            PersistenceCode.EMPTY,
            0,
            None,
            id="receipt-suppresses-unchanged-malformed-source",
        ),
        pytest.param(
            _version_one(PROTOTYPE_V1),
            (_prototype(), _receipt()),
            PersistenceCode.PROTOTYPE_IMPORTED,
            1,
            None,
            id="completed-import-relation",
        ),
        pytest.param(
            _version_one(),
            (_prototype(), _receipt()),
            PersistenceCode.CURRENT,
            0,
            None,
            id="authority-mutated-after-import",
        ),
        pytest.param(
            _version_one(),
            (_prototype(ArtifactState.MALFORMED_JSON),),
            PersistenceCode.CURRENT,
            0,
            None,
            id="stale-prototype-is-ineligible",
        ),
        *[
            pytest.param(
                _absent(),
                (_prototype(state),),
                code,
                None,
                None,
                id=state.value,
            )
            for state, code in (
                (ArtifactState.UNSAFE, PersistenceCode.UNSAFE_PERMISSIONS),
                (ArtifactState.UNREADABLE, PersistenceCode.UNREADABLE),
                (ArtifactState.DUPLICATE_KEY, PersistenceCode.DUPLICATE_KEY),
                (ArtifactState.MALFORMED_JSON, PersistenceCode.MALFORMED_JSON),
                (ArtifactState.INVALID_SCHEMA, PersistenceCode.INVALID_SCHEMA),
            )
        ],
    ],
)
def test_prototype_receipt_matrix_requires_exact_bytes(
    authority: AuthorityObservation,
    artifacts: tuple[ArtifactObservation, ...],
    code: PersistenceCode,
    count: int | None,
    command: tuple[str, ...] | None,
) -> None:
    """Import status follows exact source, receipt, and authority relations."""
    assessment = assess_persistence(_observe(authority, *artifacts))

    assert (assessment.code, assessment.account_count) == (code, count)
    assert assessment.next_command == command


def test_issue_order_is_priority_then_artifact_class_then_basename() -> None:
    """Multi-issue output is stable without exposing unsafe native details."""
    artifacts = (
        _simple_artifact(
            ArtifactKind.V0_BACKUP,
            ArtifactState.UNSAFE,
            "b.v0",
        ),
        _simple_artifact(
            ArtifactKind.PROTOTYPE_RECEIPT,
            ArtifactState.UNSAFE,
            "receipt",
        ),
        _simple_artifact(
            ArtifactKind.TEMPORARY,
            ArtifactState.UNSAFE,
            "temporary",
        ),
        _simple_artifact(
            ArtifactKind.LOCK,
            ArtifactState.UNSAFE,
            "lock",
        ),
        _simple_artifact(
            ArtifactKind.V1_SNAPSHOT,
            ArtifactState.UNSAFE,
            "snapshot",
        ),
        _simple_artifact(
            ArtifactKind.V0_BACKUP,
            ArtifactState.UNSAFE,
            "a.v0",
        ),
    )

    assessment = assess_persistence(
        _observe(AuthorityObservation(AuthorityKind.UNSAFE), *artifacts)
    )

    unsafe = tuple(
        issue.artifact_basename
        for issue in assessment.issues
        if issue.code is PersistenceCode.UNSAFE_PERMISSIONS
    )
    assert unsafe == (
        None,
        "lock",
        "a.v0",
        "b.v0",
        "snapshot",
        "temporary",
        "receipt",
    )
    assert assessment.issues[-1].code is PersistenceCode.INTERRUPTED_ARTIFACTS


def test_passive_priority_and_doctor_exit_policy_are_exact() -> None:
    """The stable 10..160 precedence also drives the documented exits."""
    expected = (
        (PersistenceCode.UNSUPPORTED_FILESYSTEM, 10, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.UNSAFE_PERMISSIONS, 20, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.UNREADABLE, 30, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.DUPLICATE_KEY, 40, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.MALFORMED_JSON, 50, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.FUTURE_SCHEMA, 60, ExitCode.MANUAL_ACTION),
        (PersistenceCode.INVALID_SCHEMA, 70, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.BACKUP_CONFLICT, 80, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.INTERRUPTED_ARTIFACTS, 90, ExitCode.MANUAL_ACTION),
        (PersistenceCode.LEGACY_WRITER_DETECTED, 100, ExitCode.MANUAL_ACTION),
        (PersistenceCode.ROLLBACK_PREPARED, 110, ExitCode.SUCCESS),
        (PersistenceCode.MIGRATION_REQUIRED, 120, ExitCode.MANUAL_ACTION),
        (
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            130,
            ExitCode.MANUAL_ACTION,
        ),
        (PersistenceCode.PROTOTYPE_IMPORTED, 140, ExitCode.SUCCESS),
        (PersistenceCode.CURRENT, 150, ExitCode.SUCCESS),
        (PersistenceCode.EMPTY, 160, ExitCode.SUCCESS),
    )

    assert (
        tuple(
            (code, passive_priority(code), doctor_exit_code(code))
            for code, _, _ in expected
        )
        == expected
    )


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        (PersistenceCode.PROTOTYPE_IMPORTED, ExitCode.SUCCESS),
        (PersistenceCode.ROLLBACK_PREPARED, ExitCode.SUCCESS),
        (PersistenceCode.ROLLBACK_REQUIRED, ExitCode.MANUAL_ACTION),
        (PersistenceCode.STORE_LOCKED, ExitCode.MANUAL_ACTION),
        (PersistenceCode.SOURCE_CHANGED, ExitCode.MANUAL_ACTION),
        (PersistenceCode.LEGACY_WRITER_DETECTED, ExitCode.MANUAL_ACTION),
        (PersistenceCode.INTERRUPTED_ARTIFACTS, ExitCode.MANUAL_ACTION),
        (PersistenceCode.REPLACE_FAILED, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.DURABILITY_UNCERTAIN, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.RESET_INCOMPLETE, ExitCode.SYSTEM_ERROR),
        (PersistenceCode.BACKUP_CONFLICT, ExitCode.SYSTEM_ERROR),
    ],
)
def test_operation_facts_do_not_replace_restart_state(
    code: PersistenceCode,
    exit_code: ExitCode,
) -> None:
    """Transient results retain, but never manufacture, passive evidence."""
    observation = _observe(_version_one())
    assessment = assess_persistence(observation)
    result = make_operation_result(code, assessment)

    assert operation_exit_code(code) is exit_code
    assert result.assessment is assessment
    assert assess_persistence(observation).code is PersistenceCode.CURRENT
    assert all(
        issue.code
        not in {
            PersistenceCode.ROLLBACK_REQUIRED,
            PersistenceCode.STORE_LOCKED,
            PersistenceCode.SOURCE_CHANGED,
            PersistenceCode.REPLACE_FAILED,
            PersistenceCode.DURABILITY_UNCERTAIN,
            PersistenceCode.RESET_INCOMPLETE,
        }
        for issue in assessment.issues
    )


def test_public_results_are_frozen_and_secret_safe() -> None:
    """Assessment output exposes authored guidance, never source secrets."""
    prototype = _prototype()
    assessment = assess_persistence(_observe(_absent(), prototype))
    result = make_operation_result(
        PersistenceCode.STORE_LOCKED,
        assessment,
        artifact_basename="accounts.lock",
    )

    assert "prototype-secret" not in repr(prototype)
    assert "prototype-secret" not in repr(assessment)
    assert "prototype-secret" not in repr(result)
    assert all(
        issue.message in repr(assessment) for issue in assessment.issues
    )
    mutate = assessment.__setattr__
    with pytest.raises(FrozenInstanceError):
        mutate("code", PersistenceCode.EMPTY)
    with pytest.raises(ValueError, match="safe basename"):
        make_operation_result(
            PersistenceCode.STORE_LOCKED,
            assessment,
            artifact_basename="../accounts.lock",
        )
    with pytest.raises(ValueError, match="Passive-only"):
        make_operation_result(PersistenceCode.CURRENT, assessment)
