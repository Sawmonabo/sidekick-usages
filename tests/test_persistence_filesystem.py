"""Load-bearing tests for qualified durable persistence operations."""

import os
from pathlib import Path

import pytest

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    FileSnapshot,
    ManagedArtifactKind,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    DurabilityUncertainError,
    InvalidManagedArtifactError,
    ReplaceFailedError,
    SourceChangedError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeReceipt,
    VersionOneDocument,
    encode_generation_zero,
    encode_prototype_receipt,
    encode_version_one,
)

GENERATION_ZERO = encode_generation_zero(GenerationZeroDocument(()))
VERSION_ONE = encode_version_one(VersionOneDocument(()))
PROTOTYPE_DIGEST = sha256_digest(b"test-only prototype")
RECEIPT = encode_prototype_receipt(PrototypeReceipt(str(PROTOTYPE_DIGEST)))
INTERRUPTED_LINK_COUNT = 2
SINGLE_LINK_COUNT = 1


def _filesystem(tmp_path: Path) -> PersistenceFilesystem:
    authority = tmp_path / "fresh" / "state" / "accounts.json"
    return PersistenceFilesystem(authority)


def _commit_initial(filesystem: PersistenceFilesystem) -> FileSnapshot:
    with PersistenceLock(filesystem).hold() as transaction:
        return transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            VERSION_ONE,
            AuthorityExpectation.ABSENT,
        )


def test_discovery_is_closed_and_foreign_names_are_never_touched(
    tmp_path: Path,
) -> None:
    filesystem = _filesystem(tmp_path)
    source = _commit_initial(filesystem)
    with PersistenceLock(filesystem).hold() as transaction:
        snapshot = transaction.publish_immutable(
            AuthorityGeneration.VERSION_ONE,
            source,
        )
    foreign = filesystem.authority_path.with_name(
        "foreign.txt" if os.name == "nt" else "ACCOUNTS.JSON"
    )
    malformed = filesystem.authority_path.with_name("accounts.json.v2.bak")
    foreign.write_bytes(b"foreign-case-variant")
    malformed.write_bytes(b"foreign-malformed-name")

    discovered = filesystem.discover_managed()

    assert {artifact.kind for artifact in discovered} == {
        ManagedArtifactKind.AUTHORITY,
        ManagedArtifactKind.LOCK,
        ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
    }
    assert snapshot in discovered
    assert foreign.read_bytes() == b"foreign-case-variant"
    assert malformed.read_bytes() == b"foreign-malformed-name"


def test_commit_revalidates_source_and_never_clobbers_first_write_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    filesystem._prepare_parent()
    native_replace = filesystem._native.replace

    def race_replace(
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        *,
        destination_exists: bool,
        device: int,
        inode: int,
    ) -> None:
        filesystem._native.create_private(
            parent,
            final_basename,
            GENERATION_ZERO,
        )
        native_replace(
            parent,
            temporary_basename,
            final_basename,
            destination_exists=destination_exists,
            device=device,
            inode=inode,
        )

    monkeypatch.setattr(filesystem._native, "replace", race_replace)

    with (
        PersistenceLock(filesystem).hold() as transaction,
        pytest.raises(SourceChangedError),
    ):
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            VERSION_ONE,
            AuthorityExpectation.ABSENT,
        )

    assert filesystem.authority_path.read_bytes() == GENERATION_ZERO
    assert not any(
        artifact.kind is ManagedArtifactKind.TEMPORARY
        for artifact in filesystem.discover_managed()
    )


def test_replace_failure_distinguishes_unchanged_source_from_committed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    baseline = _commit_initial(filesystem)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise NativeFilesystemError(NativeFailureKind.REPLACE)

    monkeypatch.setattr(filesystem._native, "replace", fail_replace)
    with (
        PersistenceLock(filesystem).hold() as transaction,
        pytest.raises(ReplaceFailedError),
    ):
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            VERSION_ONE,
            baseline.fingerprint,
        )

    assert filesystem.read_authority() == baseline
    assert not any(
        artifact.kind is ManagedArtifactKind.TEMPORARY
        for artifact in filesystem.discover_managed()
    )


def test_post_replace_hardening_failure_reports_uncertain_new_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    baseline = _commit_initial(filesystem)

    def fail_harden(
        _parent: Path,
        _basename: str,
        _limit: int,
    ) -> None:
        raise NativeFilesystemError(NativeFailureKind.HARDEN)

    monkeypatch.setattr(filesystem._native, "harden", fail_harden)
    with (
        PersistenceLock(filesystem).hold() as transaction,
        pytest.raises(DurabilityUncertainError),
    ):
        transaction.commit_authority(
            AuthorityGeneration.GENERATION_ZERO,
            GENERATION_ZERO,
            baseline.fingerprint,
        )

    assert filesystem.authority_path.read_bytes() == GENERATION_ZERO


@pytest.mark.skipif(os.name == "nt", reason="POSIX link interruption")
def test_link_publish_interruption_is_recoverable_only_through_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = _filesystem(tmp_path)
    original_unlink = os.unlink
    interrupted = False

    def interrupt_first_unlink(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("injected link-unlink interruption")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", interrupt_first_unlink)
    with (
        PersistenceLock(filesystem).hold() as transaction,
        pytest.raises(DurabilityUncertainError),
    ):
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            VERSION_ONE,
            AuthorityExpectation.ABSENT,
        )

    temporary = next(
        artifact
        for artifact in filesystem.discover_managed()
        if artifact.kind is ManagedArtifactKind.TEMPORARY
    )
    interrupted_authority = filesystem.read_authority()
    assert interrupted_authority is not None
    assert interrupted_authority.link_count == INTERRUPTED_LINK_COUNT

    with PersistenceLock(filesystem).hold() as transaction:
        transaction.recover_or_discard_temporary(temporary)

    authority = filesystem.read_authority()
    assert authority is not None
    assert authority.link_count == SINGLE_LINK_COUNT
    assert temporary not in filesystem.discover_managed()


def test_adversarial_file_shapes_fail_closed_without_blocking(
    tmp_path: Path,
) -> None:
    hardlinked = _filesystem(tmp_path / "hardlink")
    _commit_initial(hardlinked)
    os.link(
        hardlinked.authority_path,
        hardlinked.authority_path.with_name("foreign-hardlink"),
    )
    with pytest.raises(UnsafeManagedFileError):
        hardlinked.read_authority()

    if os.name != "nt":
        fifo = _filesystem(tmp_path / "fifo")
        fifo._prepare_parent()
        os.mkfifo(fifo.authority_path, 0o600)
        with pytest.raises(UnsafeManagedFileError):
            fifo.read_authority()


def test_oversize_mapping_preserves_artifact_role(tmp_path: Path) -> None:
    filesystem = _filesystem(tmp_path)
    filesystem._prepare_parent()
    oversized = b"x" * (MAX_DOCUMENT_BYTES + 1)
    filesystem._native.create_private(
        filesystem.authority_path.parent,
        filesystem.authority_path.name,
        oversized,
    )
    with pytest.raises(InvalidManagedArtifactError):
        filesystem.read_authority()

    filesystem.authority_path.unlink()
    digest = sha256_digest(oversized)
    backup = filesystem.grammar.parse(
        filesystem.grammar.backup_basename(
            AuthorityGeneration.VERSION_ONE,
            digest,
        )
    )
    assert backup is not None
    backup_path = filesystem.authority_path.with_name(backup.basename)
    filesystem._native.create_private(
        backup_path.parent,
        backup_path.name,
        oversized,
    )
    with pytest.raises(BackupConflictError):
        filesystem.read_managed(backup)


def test_full_reset_is_prevalidated_and_retains_nonsecret_state(
    tmp_path: Path,
) -> None:
    filesystem = _filesystem(tmp_path)
    baseline = _commit_initial(filesystem)
    with PersistenceLock(filesystem).hold() as transaction:
        backup = transaction.publish_immutable(
            AuthorityGeneration.VERSION_ONE,
            baseline,
        )
        receipt = transaction.publish_receipt(PROTOTYPE_DIGEST, RECEIPT)

    stale = baseline.fingerprint
    with PersistenceLock(filesystem).hold() as transaction:
        current = transaction.commit_authority(
            AuthorityGeneration.GENERATION_ZERO,
            GENERATION_ZERO,
            stale,
        )
        with pytest.raises(SourceChangedError):
            transaction.full_reset(stale)
    assert backup in filesystem.discover_managed()

    with PersistenceLock(filesystem).hold() as transaction:
        transaction.full_reset(current.fingerprint)

    remaining = filesystem.discover_managed()
    assert {artifact.kind for artifact in remaining} == {
        ManagedArtifactKind.LOCK,
        ManagedArtifactKind.PROTOTYPE_RECEIPT,
    }
    assert receipt in remaining
