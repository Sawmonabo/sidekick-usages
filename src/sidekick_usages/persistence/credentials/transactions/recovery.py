"""Strict recovery for credential transactions."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidekick_usages.persistence.credentials.transactions.plans import (
    CredentialTransactionPlan,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    SourceChangedError,
)
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
)
from sidekick_usages.persistence.private.bundles.paths import (
    PRIVATE_TRANSACTION_JOURNAL,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.transaction import (
    AbsentAuthority,
    CredentialJournal,
    CredentialSourceGuardRecord,
    CredentialTransactionFile,
    PresentAuthority,
    decode_credential_journal,
    journal_authority,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    Sha256Digest,
    sha256_digest,
)

type _AuthorityReader = Callable[[], FileSnapshot | None]


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
            bundle = self._tree.root / basename
            present = self._tree.bundle_present(bundle)
            if present:
                raise SourceChangedError
            self._tree.destroy_owned_directory(bundle)

    def _restore_record(
        self,
        record: CredentialTransactionFile,
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
            self._tree.destroy_owned_directory(self._tree.root / relative)

    def _read_record(
        self,
        record: CredentialTransactionFile,
    ) -> FileSnapshot | None:
        return self._tree.read_owned_file(
            self._tree.root / record.bundle_basename,
            record.basename,
        )

    def _install_record_artifact(
        self,
        record: CredentialTransactionFile,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        return self._tree.write_owned_file(
            self._tree.root / record.bundle_basename,
            record.basename,
            payload,
            expected_source=expected_source,
        )

    def _delete_record(
        self,
        record: CredentialTransactionFile,
        expected: FileFingerprint,
    ) -> None:
        self._tree.delete_owned_file(
            self._tree.root / record.bundle_basename,
            record.basename,
            expected,
        )

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
]
