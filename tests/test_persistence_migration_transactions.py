"""Migration-only credential transaction and recovery tests."""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence._platform import (
    posix_private,
    posix_private_bundles,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    sha256_digest,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    MigrationCredentialTransactionJournal,
    decode_credential_journal,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    DivergentSourceOutcome,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    InterruptedArtifactError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    encode_generation_zero,
    encode_version_one,
)
from sidekick_usages.persistence.transaction import PersistenceTransaction
from sidekick_usages.persistence.transforms import accounts_to_version_one
from tests.test_support import make_application_paths

_OLD_AUTH = b"test-only-old-private-auth"
_NEW_AUTH = b"test-only-new-private-auth"
_SECOND_SOURCE_READ = 2
_TARGET_SWAP_CHECKPOINT = 4


@dataclass(frozen=True, slots=True)
class _MigrationFixture:
    target: PersistenceFilesystem
    source: PersistenceFilesystem
    tree: PrivateCredentialTree
    bundle: Path
    base: FileSnapshot | None
    target_payload: bytes
    source_snapshot: FileSnapshot


class _SimulatedCrash(BaseException):
    """Process-loss signal intentionally not caught by transaction code."""


class _CrashBeforeAuthority:
    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        del generation, payload, expected_source
        raise _SimulatedCrash

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        del generation, source
        raise _SimulatedCrash


class _CrashAfterAuthority:
    def __init__(self, transaction: PersistenceTransaction) -> None:
        self._transaction = transaction

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        self._transaction.commit_authority(
            generation,
            payload,
            expected_source,
        )
        raise _SimulatedCrash

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        return self._transaction.publish_immutable(generation, source)


def _codex_account(label: str, auth_home: Path, token: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=token,
            refresh_token=f"{token}-refresh",
            account_id=f"{label}-id",
            auth_home=str(auth_home),
            id_token=f"{token}-id",
        ),
        plan="pro",
    )


def _claude_account(label: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeCredentials(access_token=f"{label}-token"),
    )


def _authority_payload(*accounts: Account) -> bytes:
    return encode_version_one(accounts_to_version_one(accounts))


def _protected_filesystem(path: Path) -> PersistenceFilesystem:
    """Build a native persistence boundary below a protected test parent."""
    filesystem = PersistenceFilesystem(path)
    filesystem.repair_parent_permissions()
    return filesystem


def _write_protected_artifact(path: Path, payload: bytes) -> None:
    """Seed one synthetic managed artifact through native protection."""
    filesystem = _protected_filesystem(path)
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            payload,
            AuthorityExpectation.ABSENT,
        )


def _seed_migration_state(
    tmp_path: Path,
    *,
    base_present: bool,
) -> _MigrationFixture:
    paths = make_application_paths(tmp_path / "canonical")
    target = _protected_filesystem(paths.accounts.canonical)
    source = _protected_filesystem(
        tmp_path / "compatibility" / "accounts.json"
    )
    source_payload = _authority_payload(_claude_account("compatibility"))
    with PersistenceLock(source).hold() as transaction:
        source_snapshot = transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            source_payload,
            AuthorityExpectation.ABSENT,
        )
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    bundle = paths.private_codex.canonical / "teams" / "primary"
    base: FileSnapshot | None = None
    if base_present:
        tree.write_bundle(
            bundle,
            {"auth.json": _OLD_AUTH},
            expected_bundle_present=False,
            expected_files={"auth.json": None},
        )
        with PersistenceLock(target).hold() as transaction:
            base = transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                _authority_payload(
                    _codex_account("primary", bundle, "old-token")
                ),
                AuthorityExpectation.ABSENT,
            )
    return _MigrationFixture(
        target,
        source,
        tree,
        bundle,
        base,
        _authority_payload(_codex_account("primary", bundle, "new-token")),
        source_snapshot,
    )


def _migration_mutation(
    fixture: _MigrationFixture,
) -> PreparedPrivateBundleWrite:
    return PreparedPrivateBundleWrite(
        fixture.bundle,
        {"auth.json": _NEW_AUTH},
        fixture.base is not None,
        {"auth.json": _OLD_AUTH if fixture.base is not None else None},
    )


def _migration_guard(
    fixture: _MigrationFixture,
    snapshot: FileSnapshot,
    *,
    path: Path | None = None,
) -> CredentialSourceGuard:
    return CredentialSourceGuard(
        path or fixture.source.authority_path,
        snapshot.fingerprint,
        fixture.source.read_authority,
    )


def _crash_migration(
    fixture: _MigrationFixture,
    *,
    after_authority: bool,
) -> None:
    expected = (
        AuthorityExpectation.ABSENT
        if fixture.base is None
        else fixture.base.fingerprint
    )
    with PersistenceLock(fixture.target).hold() as transaction:
        authority = (
            _CrashAfterAuthority(transaction)
            if after_authority
            else _CrashBeforeAuthority()
        )
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).commit_migration(
            authority,
            AuthorityGeneration.VERSION_ONE,
            fixture.target_payload,
            expected,
            base_generation=(
                AuthorityGeneration.VERSION_ONE
                if fixture.base is not None
                else None
            ),
            private_bundles=(_migration_mutation(fixture),),
            displaced_bundles=(),
            source_guard=_migration_guard(
                fixture,
                fixture.source_snapshot,
            ),
        )


def _change_migration_source(
    fixture: _MigrationFixture,
) -> FileSnapshot:
    with PersistenceLock(fixture.source).hold() as transaction:
        return transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            _authority_payload(_claude_account("released-writer")),
            fixture.source_snapshot.fingerprint,
        )


def _migration_journal(
    fixture: _MigrationFixture,
) -> MigrationCredentialTransactionJournal:
    snapshot = fixture.tree.read_owned_file(
        fixture.tree.transaction_directory,
        PRIVATE_TRANSACTION_JOURNAL,
    )
    assert snapshot is not None
    journal = decode_credential_journal(snapshot.data)
    assert isinstance(journal, MigrationCredentialTransactionJournal)
    return journal


@pytest.mark.parametrize("after_authority", [False, True])
def test_generation_zero_migration_recovers_on_both_authority_sides(
    tmp_path: Path,
    *,
    after_authority: bool,
) -> None:
    paths = make_application_paths(tmp_path / "canonical")
    target = _protected_filesystem(paths.accounts.canonical)
    source = _protected_filesystem(
        tmp_path / "compatibility" / "accounts.json"
    )
    with PersistenceLock(source).hold() as transaction:
        source_snapshot = transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            _authority_payload(_claude_account("source")),
            AuthorityExpectation.ABSENT,
        )
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=target.authority_path,
    )
    guard = CredentialSourceGuard(
        source.authority_path,
        source_snapshot.fingerprint,
        source.read_authority,
    )
    payload = encode_generation_zero(GenerationZeroDocument(()))
    coordinator = PrivateCredentialTransaction(tree, target.read_authority)
    with PersistenceLock(target).hold() as transaction:
        authority = (
            _CrashAfterAuthority(transaction)
            if after_authority
            else _CrashBeforeAuthority()
        )
        with pytest.raises(_SimulatedCrash):
            coordinator.commit_migration(
                authority,
                AuthorityGeneration.GENERATION_ZERO,
                payload,
                AuthorityExpectation.ABSENT,
                base_generation=None,
                private_bundles=(),
                displaced_bundles=(),
                source_guard=guard,
            )

    with PersistenceLock(target).hold() as transaction:
        assert PrivateCredentialTransaction(
            tree,
            target.read_authority,
        ).recover_migration(
            transaction,
            source_guard=guard,
        )
    authority = target.read_authority()
    if after_authority:
        assert authority is not None
        assert authority.data == payload
    else:
        assert authority is None
    assert not tree.transaction_directory_present()


@pytest.mark.parametrize("collision", [False, True])
def test_successful_migration_publishes_exact_target_lineage_before_cleanup(
    tmp_path: Path,
    *,
    collision: bool,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=False)
    target_digest = sha256_digest(fixture.target_payload)
    lineage = fixture.target.authority_path.with_name(
        fixture.target.grammar.backup_basename(
            AuthorityGeneration.VERSION_ONE,
            target_digest,
        )
    )
    conflicting_lineage = _authority_payload(
        _claude_account("conflicting-lineage")
    )
    if collision:
        _write_protected_artifact(
            lineage,
            conflicting_lineage,
        )
    with PersistenceLock(fixture.target).hold() as transaction:

        def commit() -> FileSnapshot:
            return PrivateCredentialTransaction(
                fixture.tree,
                fixture.target.read_authority,
            ).commit_migration(
                transaction,
                AuthorityGeneration.VERSION_ONE,
                fixture.target_payload,
                AuthorityExpectation.ABSENT,
                base_generation=None,
                private_bundles=(_migration_mutation(fixture),),
                displaced_bundles=(),
                source_guard=_migration_guard(
                    fixture,
                    fixture.source_snapshot,
                ),
            )

        if collision:
            with pytest.raises(BackupConflictError):
                commit()
        else:
            final = commit()
            assert final.data == fixture.target_payload

    if collision:
        assert lineage.read_bytes() == conflicting_lineage
        assert fixture.tree.transaction_directory_present()
    else:
        assert lineage.read_bytes() == fixture.target_payload
        assert not fixture.tree.transaction_directory_present()


@pytest.mark.skipif(os.name == "nt", reason="POSIX component swap injection")
def test_migration_install_detects_component_swap_before_target_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_migration_state(tmp_path, base_present=False)
    original = posix_private_bundles._require_chain_identity
    target_checks = 0
    escaped = tmp_path / "escaped-teams"

    def swap_target_component(
        opened: posix_private._OpenedTree,
        chain: posix_private_bundles._OpenedChain,
        *,
        final_may_be_absent: bool = False,
    ) -> None:
        nonlocal target_checks
        if chain.components == ("teams", "primary"):
            target_checks += 1
            if target_checks == _TARGET_SWAP_CHECKPOINT:
                teams = fixture.tree.root / "teams"
                teams.rename(escaped)
                replacement = fixture.tree.root / "teams" / "primary"
                replacement.mkdir(parents=True)
                for path in (replacement.parent, replacement):
                    path.chmod(0o700)
        original(
            opened,
            chain,
            final_may_be_absent=final_may_be_absent,
        )

    monkeypatch.setattr(
        posix_private_bundles,
        "_require_chain_identity",
        swap_target_component,
    )
    with (
        PersistenceLock(fixture.target).hold() as transaction,
        pytest.raises(PrivateCredentialCollisionError),
    ):
        PrivateCredentialTransaction(
            fixture.tree,
            fixture.target.read_authority,
        ).commit_migration(
            transaction,
            AuthorityGeneration.VERSION_ONE,
            fixture.target_payload,
            AuthorityExpectation.ABSENT,
            base_generation=None,
            private_bundles=(_migration_mutation(fixture),),
            displaced_bundles=(),
            source_guard=_migration_guard(
                fixture,
                fixture.source_snapshot,
            ),
        )

    assert target_checks == _TARGET_SWAP_CHECKPOINT
    assert not (escaped / "primary" / "auth.json").exists()
    assert not (fixture.bundle / "auth.json").exists()
    assert fixture.tree.transaction_directory_present()


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
