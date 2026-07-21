"""Migration source-divergence recovery tests."""

from pathlib import Path

import pytest

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    FileFingerprint,
    FileSnapshot,
    ManagedArtifactKind,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    DivergentSourceOutcome,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    InterruptedArtifactError,
    SourceChangedError,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PrivateCredentialTree,
)
from tests.test_persistence_migration_transactions import (
    _NEW_AUTH,
    _OLD_AUTH,
    _SECOND_SOURCE_READ,
    _authority_payload,
    _change_migration_source,
    _claude_account,
    _crash_migration,
    _migration_guard,
    _migration_journal,
    _seed_migration_state,
    _SimulatedCrash,
    _write_protected_artifact,
)


@pytest.mark.parametrize(
    ("checkpoint", "base_present", "expected_outcome"),
    [
        (
            "base",
            True,
            DivergentSourceOutcome.SOURCE_DIVERGED_BASE,
        ),
        (
            "target",
            False,
            DivergentSourceOutcome.SOURCE_DIVERGED_TARGET,
        ),
    ],
)
def test_divergent_migration_converges_and_publishes_lineage(
    tmp_path: Path,
    checkpoint: str,
    base_present: bool,
    expected_outcome: DivergentSourceOutcome,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=base_present)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(
            fixture,
            after_authority=checkpoint == "target",
        )
    changed_source = _change_migration_source(fixture)
    with PersistenceLock(fixture.target).hold() as transaction:
        outcome = PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(fixture, changed_source),
        )

    assert outcome is expected_outcome
    expected_authority = (
        fixture.base.data
        if expected_outcome is DivergentSourceOutcome.SOURCE_DIVERGED_BASE
        and fixture.base is not None
        else fixture.target_payload
    )
    authority = fixture.target.read_authority()
    assert authority is not None
    assert authority.data == expected_authority
    expected_private = (
        _OLD_AUTH
        if expected_outcome is DivergentSourceOutcome.SOURCE_DIVERGED_BASE
        else _NEW_AUTH
    )
    assert (
        fixture.tree.read_bundle_file(fixture.bundle, "auth.json")
        == expected_private
    )
    lineage = fixture.target.authority_path.with_name(
        fixture.target.grammar.backup_basename(
            AuthorityGeneration.VERSION_ONE,
            authority.fingerprint.digest,
        )
    )
    assert lineage.read_bytes() == expected_authority
    source = fixture.source.read_authority()
    assert source is not None
    assert source.data == changed_source.data
    assert not fixture.tree.transaction_directory_present()


def test_divergent_absent_base_rolls_back_without_inventing_lineage(
    tmp_path: Path,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=False)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(fixture, after_authority=False)
    changed_source = _change_migration_source(fixture)
    with PersistenceLock(fixture.target).hold() as transaction:
        outcome = PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(fixture, changed_source),
        )

    assert outcome is DivergentSourceOutcome.SOURCE_DIVERGED_BASE
    assert fixture.target.read_authority() is None
    assert not fixture.bundle.exists()
    assert all(
        artifact.kind is not ManagedArtifactKind.VERSION_ONE_SNAPSHOT
        for artifact in fixture.target.discover_managed()
    )
    assert not fixture.tree.transaction_directory_present()


@pytest.mark.parametrize("failure", ["wrong_path", "third_authority"])
def test_divergent_migration_rejects_unproven_authority_or_source_path(
    tmp_path: Path,
    failure: str,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=True)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(fixture, after_authority=False)
    changed_source = _change_migration_source(fixture)
    guard_path: Path | None = None
    if failure == "wrong_path":
        guard_path = fixture.source.authority_path.with_name("other.json")
    else:
        current = fixture.target.read_authority()
        assert current is not None
        with PersistenceLock(fixture.target).hold() as transaction:
            transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                _authority_payload(_claude_account("third")),
                current.fingerprint,
            )
    with (
        PersistenceLock(fixture.target).hold() as transaction,
        pytest.raises(SourceChangedError),
    ):
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(
                fixture,
                changed_source,
                path=guard_path,
            ),
        )
    assert fixture.tree.transaction_directory_present()


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        ("private_third", SourceChangedError),
        ("missing_backup", InterruptedArtifactError),
        ("snapshot_collision", BackupConflictError),
    ],
)
def test_divergent_migration_retains_evidence_on_private_uncertainty(
    tmp_path: Path,
    failure: str,
    error_type: type[Exception],
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=True)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(
            fixture,
            after_authority=failure == "snapshot_collision",
        )
    journal = _migration_journal(fixture)
    if failure == "private_third":
        current = fixture.tree.read_relative_bundle_file(
            "teams/primary",
            "auth.json",
        )
        assert current is not None
        fixture.tree.write_owned_file(
            fixture.bundle,
            "auth.json",
            b"test-only-third-private-auth",
            expected_source=current.fingerprint,
        )
    elif failure == "missing_backup":
        backup_basename = journal.files[0].backup_basename
        assert backup_basename is not None
        backup = fixture.tree.read_owned_file(
            fixture.tree.transaction_directory,
            backup_basename,
        )
        assert backup is not None
        fixture.tree.delete_owned_file(
            fixture.tree.transaction_directory,
            backup_basename,
            backup.fingerprint,
        )
    else:
        authority = fixture.target.read_authority()
        assert authority is not None
        lineage = fixture.target.authority_path.with_name(
            fixture.target.grammar.backup_basename(
                AuthorityGeneration.VERSION_ONE,
                authority.fingerprint.digest,
            )
        )
        _write_protected_artifact(
            lineage,
            _authority_payload(_claude_account("conflicting-lineage")),
        )
    changed_source = _change_migration_source(fixture)

    with (
        PersistenceLock(fixture.target).hold() as transaction,
        pytest.raises(error_type),
    ):
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(fixture, changed_source),
        )

    assert fixture.tree.transaction_directory_present()
    source = fixture.source.read_authority()
    assert source is not None
    assert source.data == changed_source.data


def test_divergent_migration_retains_journal_on_second_source_change(
    tmp_path: Path,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=True)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(fixture, after_authority=True)
    changed_source = _change_migration_source(fixture)
    reads = 0

    def change_on_second_read() -> FileSnapshot | None:
        nonlocal reads
        reads += 1
        if reads == _SECOND_SOURCE_READ:
            current = fixture.source.read_authority()
            assert current is not None
            with PersistenceLock(fixture.source).hold() as transaction:
                transaction.commit_authority(
                    AuthorityGeneration.VERSION_ONE,
                    _authority_payload(_claude_account("second-change")),
                    current.fingerprint,
                )
        return fixture.source.read_authority()

    changing_guard = CredentialSourceGuard(
        fixture.source.authority_path,
        changed_source.fingerprint,
        change_on_second_read,
    )
    with (
        PersistenceLock(fixture.target).hold() as transaction,
        pytest.raises(SourceChangedError),
    ):
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=changing_guard,
        )

    assert reads == _SECOND_SOURCE_READ
    assert fixture.tree.transaction_directory_present()
    authority = fixture.target.read_authority()
    assert authority is not None
    lineage = fixture.target.authority_path.with_name(
        fixture.target.grammar.backup_basename(
            AuthorityGeneration.VERSION_ONE,
            authority.fingerprint.digest,
        )
    )
    assert lineage.read_bytes() == authority.data


def test_divergent_target_resumes_after_lineage_and_artifact_cleanup_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=False)
    with pytest.raises(_SimulatedCrash):
        _crash_migration(fixture, after_authority=True)
    changed_source = _change_migration_source(fixture)
    original_delete = PrivateCredentialTree.delete_owned_file

    def crash_on_journal_removal(
        self: PrivateCredentialTree,
        directory: Path,
        basename: str,
        expected: FileFingerprint,
    ) -> None:
        if basename == PRIVATE_TRANSACTION_JOURNAL:
            raise _SimulatedCrash
        original_delete(self, directory, basename, expected)

    monkeypatch.setattr(
        PrivateCredentialTree,
        "delete_owned_file",
        crash_on_journal_removal,
    )
    with (
        PersistenceLock(fixture.target).hold() as transaction,
        pytest.raises(_SimulatedCrash),
    ):
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(fixture, changed_source),
        )
    assert fixture.tree.transaction_directory_present()
    journal = _migration_journal(fixture)
    for record in journal.files:
        assert (
            fixture.tree.read_owned_file(
                fixture.tree.transaction_directory,
                record.stage_basename,
            )
            is None
        )

    monkeypatch.setattr(
        PrivateCredentialTree,
        "delete_owned_file",
        original_delete,
    )
    with PersistenceLock(fixture.target).hold() as transaction:
        outcome = PrivateCredentialTransaction(
            PrivateCredentialTree(
                fixture.tree.root,
                account_path=fixture.target.authority_path,
            ),
            fixture.target.read_authority,
        ).resolve_migration_source_divergence(
            transaction,
            source_guard=_migration_guard(fixture, changed_source),
        )

    assert outcome is DivergentSourceOutcome.SOURCE_DIVERGED_TARGET
    assert not fixture.tree.transaction_directory_present()
