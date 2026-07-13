"""Durable full-reset coordinator behavior tests."""

from pathlib import Path

import pytest

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    ManagedArtifactKind,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    ResetIncompleteError,
)
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.migrations.errors import (
    PersistenceMigrationStateError,
)
from tests.test_persistence_coordinator import (
    EMPTY_VERSION_TWO,
    GENERATION_ZERO,
    PROTOTYPE,
    VERSION_TWO,
    RecordingPrivateCredentials,
    _service,
    _snapshot,
)


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("absent", PersistenceCode.EMPTY),
        ("empty-v2", PersistenceCode.EMPTY),
        ("generation-zero", PersistenceCode.EMPTY),
        ("malformed-with-prototype", PersistenceCode.MALFORMED_JSON),
    ],
)
def test_full_reset_runs_at_zero_and_clears_validated_credentials(
    tmp_path: Path,
    state: str,
    expected_code: PersistenceCode,
) -> None:
    """Reset is authority-last recovery, including zero-account states."""
    payload = {
        "absent": None,
        "empty-v2": EMPTY_VERSION_TWO,
        "generation-zero": GENERATION_ZERO,
        "malformed-with-prototype": b"{",
    }[state]
    prototype_payload = b"{" if state == "malformed-with-prototype" else None
    service, authority, _prototype, log, _scheduler, _verifier = _service(
        tmp_path,
        payload,
        prototype_payload=prototype_payload,
    )
    if state == "generation-zero":
        authority.seed_immutable(
            AuthorityGeneration.GENERATION_ZERO,
            GENERATION_ZERO,
        )
        authority.seed_temporary("3" * 32)

    result = service.full_reset()

    assert result.code is expected_code
    assert "reset" in log
    assert authority.snapshot is None
    assert not any(
        artifact.kind
        in {
            ManagedArtifactKind.GENERATION_ZERO_BACKUP,
            ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ManagedArtifactKind.VERSION_TWO_SNAPSHOT,
            ManagedArtifactKind.TEMPORARY,
        }
        for artifact in authority.managed
    )


def test_full_reset_rejects_invalid_managed_receipt_without_deletion(
    tmp_path: Path,
) -> None:
    """External prototype errors are tolerated; managed conflicts are not."""
    service, authority, _prototype, log, _scheduler, _verifier = _service(
        tmp_path,
        VERSION_TWO,
    )
    digest = sha256_digest(PROTOTYPE)
    receipt_name = authority.grammar.receipt_basename(digest)
    receipt = authority.grammar.parse(receipt_name)
    assert receipt is not None
    authority.managed[receipt] = _snapshot(b"{}", inode=88)
    before = authority.snapshot

    with pytest.raises(PersistenceMigrationStateError) as exc_info:
        service.full_reset()

    assert exc_info.value.code is PersistenceCode.INVALID_SCHEMA
    assert authority.snapshot == before
    assert log == []


def test_full_reset_destroys_private_credentials_before_absent_authority(
    tmp_path: Path,
) -> None:
    """An orphan-only reset proves absence before authority cleanup."""
    events: list[str] = []
    private_credentials = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.PRESENT,
        events=events,
    )
    service, authority, _, _, _, _ = _service(
        tmp_path,
        None,
        private_credentials=private_credentials,
        operation_log=events,
    )

    result = service.full_reset()

    assert result.code is PersistenceCode.EMPTY
    assert authority.snapshot is None
    assert events == [
        "credentials:observe",
        "credentials:destroy",
        "credentials:observe",
        "reset",
        "credentials:observe",
    ]


@pytest.mark.parametrize(
    "failure",
    ["destroy", "verification", "passive-verification"],
)
def test_full_reset_private_credential_failure_preserves_authority(
    tmp_path: Path,
    failure: str,
) -> None:
    """Destruction and its absence proof both fail before authority."""
    private_credentials = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.PRESENT,
        fail_destroy=failure == "destroy",
        remain_present=failure == "verification",
        fail_observe_at=2 if failure == "passive-verification" else None,
    )
    service, authority, _, log, _, _ = _service(
        tmp_path,
        VERSION_TWO,
        private_credentials=private_credentials,
    )
    before = authority.snapshot

    with pytest.raises(ResetIncompleteError) as exc_info:
        service.full_reset()

    assert str(exc_info.value) == (
        "Account reset could not remove every credential artifact."
    )
    assert authority.snapshot == before
    assert log == []


def test_full_reset_translates_final_observation_failure(
    tmp_path: Path,
) -> None:
    """A post-authority absence proof cannot masquerade as passive state."""
    private_credentials = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.PRESENT,
        fail_observe_at=3,
    )
    service, authority, _, log, _, _ = _service(
        tmp_path,
        VERSION_TWO,
        private_credentials=private_credentials,
    )

    with pytest.raises(ResetIncompleteError):
        service.full_reset()

    assert authority.snapshot is None
    assert log == ["reset"]


def test_full_reset_reports_partial_private_destruction(
    tmp_path: Path,
) -> None:
    """Authority drift after private deletion is a partial reset failure."""
    private_credentials = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.PRESENT
    )
    service, authority, _, log, _, _ = _service(
        tmp_path,
        VERSION_TWO,
        private_credentials=private_credentials,
    )
    private_credentials.after_destroy = lambda: setattr(
        authority,
        "snapshot",
        _snapshot(VERSION_TWO, inode=999),
    )

    with pytest.raises(ResetIncompleteError):
        service.full_reset()

    assert private_credentials.state is OrphanedPrivateCredentials.ABSENT
    assert authority.snapshot is not None
    assert log == []
