"""Strict runtime and migration credential transaction recovery."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.credential_transaction_plans import (
    CredentialTransactionPlan,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    AbsentAuthority,
    CredentialJournal,
    CredentialSourceGuardRecord,
    CredentialTransactionFile,
    CredentialTransactionJournal,
    MigrationCredentialTransactionFile,
    MigrationCredentialTransactionJournal,
    PresentAuthority,
    decode_credential_journal,
    journal_authority,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    SourceChangedError,
)
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PrivateCredentialTree,
)

type _AuthorityReader = Callable[[], FileSnapshot | None]


class _LineagePublisher(Protocol):
    """Held-lock capability for content-addressed lineage publication."""

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish or reuse one exact content-addressed lineage snapshot."""


@dataclass(frozen=True, slots=True)
class CredentialSourceGuard:
    """Exact retained authority checked around a distinct target commit."""

    path: Path
    expected: ExpectedAuthority
    reader: _AuthorityReader = field(repr=False)

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("Credential source guard path must be absolute.")
        if not self.path.name:
            raise ValueError("Credential source guard requires a basename.")


class _AuthorityState(StrEnum):
    BASE = "base"
    TARGET = "target"
    THIRD = "third"


class DivergentSourceOutcome(StrEnum):
    """Closed successful outcomes for migration-only source divergence."""

    SOURCE_DIVERGED_BASE = "source_diverged_base"
    SOURCE_DIVERGED_TARGET = "source_diverged_target"


def _expected_source(snapshot: FileSnapshot | None) -> ExpectedAuthority:
    if snapshot is None:
        return AuthorityExpectation.ABSENT
    return snapshot.fingerprint


class CredentialTransactionRecovery:
    """Resolve credential journals through one bound private tree."""

    def __init__(
        self,
        tree: PrivateCredentialTree,
        authority_reader: _AuthorityReader,
    ) -> None:
        self._tree = tree
        self._authority_reader = authority_reader

    def recover(
        self,
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> bool:
        """Resolve one interrupted transaction from fresh filesystem state.

        :returns: Whether transaction evidence was found and resolved.
        """
        if not self._tree.transaction_directory_present():
            return False
        journal_snapshot = self._tree.read_owned_file(
            self._tree.transaction_directory,
            PRIVATE_TRANSACTION_JOURNAL,
        )
        if journal_snapshot is None:
            if self._tree.transaction_artifacts_present():
                raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
            self._tree.destroy_owned_directory(
                self._tree.transaction_directory
            )
            return True
        journal = decode_credential_journal(journal_snapshot.data)
        if isinstance(journal, MigrationCredentialTransactionJournal):
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        self.require_source_guard(journal.source_guard, source_guard)
        state = self._authority_state(journal, self._authority_reader())
        if state is _AuthorityState.BASE:
            self._restore_base(journal)
        elif state is _AuthorityState.TARGET:
            self._ensure_target(journal)
            self.delete_displaced(journal)
        else:
            raise SourceChangedError
        self.cleanup_transaction(journal)
        return True

    def recover_migration(
        self,
        transaction: _LineagePublisher,
        *,
        source_guard: CredentialSourceGuard,
    ) -> bool:
        """Strictly recover one version-two migration and publish lineage.

        :param transaction: Capability for canonical lineage publication.
        :param source_guard: Original unchanged compatibility authority.
        :returns: Whether transaction evidence was found and resolved.
        """
        if not self._tree.transaction_directory_present():
            return False
        journal_snapshot = self._tree.read_owned_file(
            self._tree.transaction_directory,
            PRIVATE_TRANSACTION_JOURNAL,
        )
        if journal_snapshot is None:
            if self._tree.transaction_artifacts_present():
                raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
            self._tree.destroy_owned_directory(
                self._tree.transaction_directory
            )
            return True
        journal = decode_credential_journal(journal_snapshot.data)
        if not isinstance(journal, MigrationCredentialTransactionJournal):
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        self.require_source_guard(journal.source_guard, source_guard)
        state = self._authority_state(journal, self._authority_reader())
        if state is _AuthorityState.BASE:
            self._restore_base(journal)
        elif state is _AuthorityState.TARGET:
            self._ensure_target(journal)
            self.delete_displaced(journal)
            coherent = self.require_coherent_authority(journal, state)
            if coherent is None:
                raise SourceChangedError
            transaction.publish_immutable(
                journal.target_generation,
                coherent,
            )
        else:
            raise SourceChangedError
        self.require_source_guard(journal.source_guard, source_guard)
        self.require_coherent_authority(journal, state)
        self.require_private_state(journal, state)
        self.cleanup_transaction_artifacts(journal)
        self.require_source_guard(journal.source_guard, source_guard)
        self.require_coherent_authority(journal, state)
        self.require_private_state(journal, state)
        self._finish_transaction_cleanup(journal)
        return True

    def resolve_migration_source_divergence(
        self,
        transaction: _LineagePublisher,
        *,
        source_guard: CredentialSourceGuard,
    ) -> DivergentSourceOutcome:
        """Resolve one version-two journal after its source changed.

        :param transaction: Capability for canonical lineage publication.
        :param source_guard: Fresh exact state at the journal's source path.
        :returns: Whether canonical state converged to base or target.
        """
        journal = self._read_migration_journal()
        self._require_divergent_source_guard(journal, source_guard)
        state = self._authority_state(journal, self._authority_reader())
        if state is _AuthorityState.BASE:
            self._restore_base(journal)
            coherent = self.require_coherent_authority(journal, state)
            if isinstance(journal.base_authority, PresentAuthority):
                if coherent is None or journal.base_generation is None:
                    raise SourceChangedError
                transaction.publish_immutable(
                    journal.base_generation,
                    coherent,
                )
            elif coherent is not None:
                raise SourceChangedError
            outcome = DivergentSourceOutcome.SOURCE_DIVERGED_BASE
        elif state is _AuthorityState.TARGET:
            self._ensure_target(journal)
            self.delete_displaced(journal)
            coherent = self.require_coherent_authority(journal, state)
            if coherent is None:
                raise SourceChangedError
            transaction.publish_immutable(
                journal.target_generation,
                coherent,
            )
            outcome = DivergentSourceOutcome.SOURCE_DIVERGED_TARGET
        else:
            raise SourceChangedError
        self.require_private_state(journal, state)
        self._require_divergent_source_guard(journal, source_guard)
        self.require_coherent_authority(journal, state)
        self.cleanup_transaction_artifacts(journal)
        self._require_divergent_source_guard(journal, source_guard)
        self.require_coherent_authority(journal, state)
        self.require_private_state(journal, state)
        self._finish_transaction_cleanup(journal)
        return outcome

    @staticmethod
    def source_guard_record(
        guard: CredentialSourceGuard | None,
    ) -> CredentialSourceGuardRecord | None:
        if guard is None:
            return None
        path_digest = sha256_digest(str(guard.path).encode("utf-8"))
        return CredentialSourceGuardRecord(
            path_sha256=str(path_digest),
            authority=journal_authority(guard.expected),
        )

    @classmethod
    def require_source_guard(
        cls,
        recorded: CredentialSourceGuardRecord | None,
        guard: CredentialSourceGuard | None,
    ) -> None:
        if recorded is None:
            if guard is not None:
                raise SourceChangedError
            return
        if guard is None or cls.source_guard_record(guard) != recorded:
            raise SourceChangedError
        cls._require_observed_guard(guard)

    def apply_target(self, plan: CredentialTransactionPlan) -> None:
        journal = plan.journal
        for planned in plan.files:
            stage = self._required_transaction_file(
                planned.record.stage_basename,
                Sha256Digest(planned.record.target_sha256),
            )
            final = self._install_record_artifact(
                planned.record,
                planned.record.stage_basename,
                stage.data,
                _expected_source(planned.base),
            )
            if final.fingerprint.digest != Sha256Digest(
                planned.record.target_sha256
            ):
                raise SourceChangedError
        self._require_target_files(journal)

    def _restore_base(self, journal: CredentialJournal) -> None:
        for record in journal.files:
            self._restore_record(record)
        for basename in sorted(
            set(journal.target_bundles) - set(journal.base_present_bundles)
        ):
            bundle = self._bundle_for_journal_path(journal, basename)
            present = (
                self._tree.bundle_present(bundle)
                if isinstance(journal, CredentialTransactionJournal)
                else self._tree.relative_bundle_present(basename)
            )
            if present:
                raise SourceChangedError
            if isinstance(journal, CredentialTransactionJournal):
                self._tree.destroy_owned_directory(bundle)
            else:
                self._tree.destroy_relative_bundle(basename)

    def _restore_record(
        self,
        record: CredentialTransactionFile | MigrationCredentialTransactionFile,
    ) -> None:
        current = self._read_record(record)
        target_digest = Sha256Digest(record.target_sha256)
        base_digest = (
            Sha256Digest(record.base_sha256)
            if record.base_sha256 is not None
            else None
        )
        if base_digest is None:
            if current is None:
                return
            if current.fingerprint.digest != target_digest:
                raise SourceChangedError
            self._delete_record(record, current.fingerprint)
            return
        if current is not None and current.fingerprint.digest == base_digest:
            return
        if current is None or current.fingerprint.digest != target_digest:
            raise SourceChangedError
        if record.backup_basename is None:
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        backup = self._required_transaction_file(
            record.backup_basename,
            base_digest,
        )
        restored = self._install_record_artifact(
            record,
            record.backup_basename,
            backup.data,
            current.fingerprint,
        )
        if restored.fingerprint.digest != base_digest:
            raise SourceChangedError

    def _ensure_target(self, journal: CredentialJournal) -> None:
        for record in journal.files:
            current = self._read_record(record)
            target_digest = Sha256Digest(record.target_sha256)
            if (
                current is not None
                and current.fingerprint.digest == target_digest
            ):
                continue
            base_digest = (
                Sha256Digest(record.base_sha256)
                if record.base_sha256 is not None
                else None
            )
            if current is not None and (
                base_digest is None
                or current.fingerprint.digest != base_digest
            ):
                raise SourceChangedError
            stage = self._required_transaction_file(
                record.stage_basename,
                target_digest,
            )
            final = self._install_record_artifact(
                record,
                record.stage_basename,
                stage.data,
                _expected_source(current),
            )
            if final.fingerprint.digest != target_digest:
                raise SourceChangedError
        self._require_target_files(journal)

    def _require_target_files(
        self,
        journal: CredentialJournal,
    ) -> None:
        for record in journal.files:
            current = self._read_record(record)
            if current is None or current.fingerprint.digest != Sha256Digest(
                record.target_sha256
            ):
                raise SourceChangedError

    def _require_base_files(self, journal: CredentialJournal) -> None:
        for record in journal.files:
            current = self._read_record(record)
            expected = (
                None
                if record.base_sha256 is None
                else Sha256Digest(record.base_sha256)
            )
            if expected is None:
                if current is not None:
                    raise SourceChangedError
            elif current is None or current.fingerprint.digest != expected:
                raise SourceChangedError
        for relative in set(journal.target_bundles) - set(
            journal.base_present_bundles
        ):
            if self._bundle_present(journal, relative):
                raise SourceChangedError

    def require_private_state(
        self,
        journal: CredentialJournal,
        state: _AuthorityState,
    ) -> None:
        if state is _AuthorityState.BASE:
            self._require_base_files(journal)
            return
        self._require_target_files(journal)
        if any(
            self._bundle_present(journal, relative)
            for relative in journal.displaced_bundles
        ):
            raise SourceChangedError

    def require_target_state(
        self,
        journal: CredentialJournal,
    ) -> FileSnapshot:
        """Require coherent target authority and private state."""
        current = self.require_coherent_authority(
            journal,
            _AuthorityState.TARGET,
        )
        if current is None:
            raise SourceChangedError
        self.require_private_state(journal, _AuthorityState.TARGET)
        return current

    def _bundle_present(
        self,
        journal: CredentialJournal,
        relative: str,
    ) -> bool:
        if isinstance(journal, CredentialTransactionJournal):
            return self._tree.bundle_present(self._tree.root / relative)
        return self._tree.relative_bundle_present(relative)

    def require_coherent_authority(
        self,
        journal: CredentialJournal,
        state: _AuthorityState,
    ) -> FileSnapshot | None:
        current = self._authority_reader()
        if self._authority_state(journal, current) is not state:
            raise SourceChangedError
        return current

    def _required_transaction_file(
        self,
        basename: str,
        digest: Sha256Digest,
    ) -> FileSnapshot:
        snapshot = self._tree.read_owned_file(
            self._tree.transaction_directory,
            basename,
        )
        if snapshot is None or snapshot.fingerprint.digest != digest:
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        return snapshot

    def delete_displaced(
        self,
        journal: CredentialJournal,
    ) -> None:
        for relative in journal.displaced_bundles:
            if isinstance(journal, CredentialTransactionJournal):
                self._tree.destroy_owned_directory(
                    self._bundle_for_journal_path(journal, relative)
                )
            else:
                self._tree.destroy_relative_bundle(relative)

    def _read_record(
        self,
        record: CredentialTransactionFile | MigrationCredentialTransactionFile,
    ) -> FileSnapshot | None:
        if isinstance(record, CredentialTransactionFile):
            return self._tree.read_owned_file(
                self._tree.root / record.bundle_basename,
                record.basename,
            )
        return self._tree.read_relative_bundle_file(
            record.bundle_path,
            record.basename,
        )

    def _install_record_artifact(
        self,
        record: CredentialTransactionFile | MigrationCredentialTransactionFile,
        artifact_basename: str,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        if isinstance(record, CredentialTransactionFile):
            return self._tree.write_owned_file(
                self._tree.root / record.bundle_basename,
                record.basename,
                payload,
                expected_source=expected_source,
            )
        return self._tree.install_staged_bundle_file(
            record.bundle_path,
            record.basename,
            artifact_basename,
            expected_source=expected_source,
        )

    def _delete_record(
        self,
        record: CredentialTransactionFile | MigrationCredentialTransactionFile,
        expected: FileFingerprint,
    ) -> None:
        if isinstance(record, CredentialTransactionFile):
            self._tree.delete_owned_file(
                self._tree.root / record.bundle_basename,
                record.basename,
                expected,
            )
            return
        self._tree.delete_relative_bundle_file(
            record.bundle_path,
            record.basename,
            expected,
        )

    def _bundle_for_record(
        self,
        record: CredentialTransactionFile | MigrationCredentialTransactionFile,
    ) -> Path:
        if isinstance(record, CredentialTransactionFile):
            return self._tree.root / record.bundle_basename
        return self._tree.canonical_bundle_path(record.bundle_path)

    def _bundle_for_journal_path(
        self,
        journal: CredentialJournal,
        value: str,
    ) -> Path:
        if isinstance(journal, CredentialTransactionJournal):
            return self._tree.root / value
        return self._tree.canonical_bundle_path(value)

    def cleanup_transaction(
        self,
        journal: CredentialJournal,
    ) -> None:
        self.cleanup_transaction_artifacts(journal)
        self._finish_transaction_cleanup(journal)

    def cleanup_transaction_artifacts(
        self,
        journal: CredentialJournal,
    ) -> None:
        directory = self._tree.transaction_directory
        for record in journal.files:
            for basename in (
                record.stage_basename,
                record.backup_basename,
            ):
                if basename is None:
                    continue
                snapshot = self._tree.read_owned_file(directory, basename)
                if snapshot is not None:
                    self._tree.delete_owned_file(
                        directory,
                        basename,
                        snapshot.fingerprint,
                    )

    def _finish_transaction_cleanup(
        self,
        journal: CredentialJournal,
    ) -> None:
        directory = self._tree.transaction_directory
        snapshot = self._tree.read_owned_file(
            directory,
            PRIVATE_TRANSACTION_JOURNAL,
        )
        if snapshot is None or (
            decode_credential_journal(snapshot.data) != journal
        ):
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        self._tree.delete_owned_file(
            directory,
            PRIVATE_TRANSACTION_JOURNAL,
            snapshot.fingerprint,
        )
        self._tree.destroy_owned_directory(directory)

    def _read_migration_journal(
        self,
    ) -> MigrationCredentialTransactionJournal:
        if not self._tree.transaction_directory_present():
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        snapshot = self._tree.read_owned_file(
            self._tree.transaction_directory,
            PRIVATE_TRANSACTION_JOURNAL,
        )
        if snapshot is None:
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        journal = decode_credential_journal(snapshot.data)
        if not isinstance(journal, MigrationCredentialTransactionJournal):
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        return journal

    @classmethod
    def _require_divergent_source_guard(
        cls,
        journal: MigrationCredentialTransactionJournal,
        guard: CredentialSourceGuard,
    ) -> None:
        current = cls.source_guard_record(guard)
        if current is None or (
            current.path_sha256 != journal.source_guard.path_sha256
            or current.authority == journal.source_guard.authority
        ):
            raise SourceChangedError
        cls._require_observed_guard(guard)

    @staticmethod
    def _require_observed_guard(guard: CredentialSourceGuard) -> None:
        observed = guard.reader()
        if guard.expected is AuthorityExpectation.ABSENT:
            if observed is not None:
                raise SourceChangedError
        elif observed is None or observed.fingerprint != guard.expected:
            raise SourceChangedError

    @staticmethod
    def _authority_state(
        journal: CredentialJournal,
        current: FileSnapshot | None,
    ) -> _AuthorityState:
        base = journal.base_authority
        if isinstance(base, AbsentAuthority):
            if current is None:
                return _AuthorityState.BASE
        elif isinstance(base, PresentAuthority) and current is not None:
            expected = FileFingerprint(
                FileIdentity(base.device, base.inode),
                Sha256Digest(base.sha256),
                base.size,
            )
            if current.fingerprint == expected:
                return _AuthorityState.BASE
        if current is not None and (
            current.fingerprint.digest
            == Sha256Digest(journal.target_authority_sha256)
            and current.fingerprint.size == journal.target_authority_size
        ):
            return _AuthorityState.TARGET
        return _AuthorityState.THIRD


__all__ = [
    "CredentialSourceGuard",
    "CredentialTransactionRecovery",
    "DivergentSourceOutcome",
]
