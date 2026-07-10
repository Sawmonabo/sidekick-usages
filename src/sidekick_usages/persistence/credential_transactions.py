"""Crash-recoverable account-authority and private-bundle coordination."""

from collections.abc import Callable, Iterable
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
    Sha256Digest,
    portable_basename_key,
    require_portable_unique_basenames,
    sha256_digest,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    AbsentAuthority,
    CredentialSourceGuardRecord,
    CredentialTransactionFile,
    CredentialTransactionJournal,
    PresentAuthority,
    decode_credential_journal,
    encode_credential_journal,
    journal_authority,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PreparedPrivateBundleWrite,
    PrivateCredentialOwnership,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import MAX_ACCOUNTS


class _AuthorityTransaction(Protocol):
    """Held-lock authority operation used at the final commit point."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact authoritative bytes."""


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


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    record: CredentialTransactionFile
    target: bytes = field(repr=False)
    base: FileSnapshot | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class _TransactionPlan:
    journal: CredentialTransactionJournal
    files: tuple[_PlannedFile, ...] = field(repr=False)


def _expected_source(snapshot: FileSnapshot | None) -> ExpectedAuthority:
    if snapshot is None:
        return AuthorityExpectation.ABSENT
    return snapshot.fingerprint


class PrivateCredentialTransaction:
    """Coordinate private bytes and one version-one authority under a lock."""

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
        self._require_source_guard(journal.source_guard, source_guard)
        state = self._authority_state(journal, self._authority_reader())
        if state is _AuthorityState.BASE:
            self._restore_base(journal)
        elif state is _AuthorityState.TARGET:
            self._ensure_target(journal)
            self._delete_displaced(journal)
        else:
            raise SourceChangedError
        self._cleanup_transaction(journal)
        return True

    def commit(
        self,
        transaction: _AuthorityTransaction,
        payload: bytes,
        expected_source: ExpectedAuthority,
        *,
        private_bundles: tuple[PreparedPrivateBundleWrite, ...],
        displaced_bundles: Iterable[Path],
        source_guard: CredentialSourceGuard | None = None,
    ) -> FileSnapshot:
        """Commit private changes first and version-one authority last.

        :param transaction: Capability proving the account lock is held.
        :param payload: Canonical target version-one authority bytes.
        :param expected_source: Exact old authority expectation.
        :param private_bundles: Bounded private target mutations.
        :param displaced_bundles: Old canonical homes no longer referenced.
        :param source_guard: Optional distinct authority retained unchanged.
        :returns: Reopened and verified target authority.
        """
        if self._tree.transaction_directory_present():
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        displaced = self._validated_displaced(displaced_bundles)
        if not private_bundles and not displaced and source_guard is None:
            return transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                payload,
                expected_source,
            )
        self._require_authority(expected_source)
        self._require_source_guard(
            self._source_guard_record(source_guard),
            source_guard,
        )
        plan = self._build_plan(
            payload,
            expected_source,
            private_bundles,
            displaced,
            source_guard,
        )
        self._write_journal(plan.journal)
        try:
            self._materialize_private_candidates(plan)
            self._apply_target(plan)
            self._require_source_guard(plan.journal.source_guard, source_guard)
            final = transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                payload,
                expected_source,
            )
            if (
                final.fingerprint.digest
                != Sha256Digest(plan.journal.target_authority_sha256)
                or final.fingerprint.size != plan.journal.target_authority_size
            ):
                raise SourceChangedError
            self._require_source_guard(plan.journal.source_guard, source_guard)
            self._delete_displaced(plan.journal)
            self._cleanup_transaction(plan.journal)
            return final
        except Exception:
            self.recover(source_guard=source_guard)
            raise

    def _build_plan(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
        bundles: tuple[PreparedPrivateBundleWrite, ...],
        displaced: tuple[Path, ...],
        source_guard: CredentialSourceGuard | None,
    ) -> _TransactionPlan:
        planned: list[_PlannedFile] = []
        if len(bundles) > MAX_ACCOUNTS:
            raise ValueError("Too many prepared private bundles.")
        bundle_names = tuple(bundle.path.name for bundle in bundles)
        require_portable_unique_basenames(bundle_names)
        next_index = 0
        for bundle in sorted(bundles, key=lambda item: item.path.name):
            self._require_canonical_bundle(bundle.path)
            present = self._tree.bundle_present(bundle.path)
            if present is not bundle.expected_bundle_present:
                raise PrivateCredentialCollisionError(bundle.path.name)
            for basename, target in sorted(bundle.files.items()):
                base = (
                    self._tree.read_owned_file(bundle.path, basename)
                    if present
                    else None
                )
                if basename in bundle.expected_files:
                    expected = bundle.expected_files[basename]
                    if (base is None) is not (expected is None) or (
                        base is not None and base.data != expected
                    ):
                        raise PrivateCredentialCollisionError(bundle.path.name)
                record = CredentialTransactionFile(
                    bundle_basename=bundle.path.name,
                    basename=basename,
                    stage_basename=f"stage-{next_index:04d}.bin",
                    backup_basename=(
                        f"backup-{next_index:04d}.bin"
                        if base is not None
                        else None
                    ),
                    base_sha256=(
                        str(base.fingerprint.digest)
                        if base is not None
                        else None
                    ),
                    target_sha256=str(sha256_digest(target)),
                )
                planned.append(_PlannedFile(record, target, base))
                next_index += 1
        journal = CredentialTransactionJournal(
            journal_version=1,
            base_authority=journal_authority(expected_source),
            source_guard=self._source_guard_record(source_guard),
            target_authority_sha256=str(sha256_digest(payload)),
            target_authority_size=len(payload),
            target_bundles=tuple(sorted(bundle_names)),
            base_present_bundles=tuple(
                sorted(
                    bundle.path.name
                    for bundle in bundles
                    if bundle.expected_bundle_present
                )
            ),
            files=tuple(item.record for item in planned),
            displaced_bundles=tuple(path.name for path in displaced),
        )
        encode_credential_journal(journal)
        return _TransactionPlan(journal, tuple(planned))

    def _validated_displaced(
        self,
        bundles: Iterable[Path],
    ) -> tuple[Path, ...]:
        unique: dict[str, Path] = {}
        for bundle in bundles:
            self._require_canonical_bundle(bundle)
            key = portable_basename_key(bundle.name)
            if key in unique:
                raise ValueError(
                    "Displaced private bundle paths must be unique."
                )
            unique[key] = bundle
        if len(unique) > MAX_ACCOUNTS:
            raise ValueError("Too many displaced private bundles.")
        return tuple(unique[key] for key in sorted(unique))

    def _require_canonical_bundle(self, path: Path) -> None:
        if (
            self._tree.classify_bundle(path)
            is not PrivateCredentialOwnership.CANONICAL
        ):
            raise ValueError("Private bundle is not canonically owned.")

    def _require_authority(self, expected: ExpectedAuthority) -> None:
        current = self._authority_reader()
        if expected is AuthorityExpectation.ABSENT:
            if current is not None:
                raise SourceChangedError
        elif current is None or current.fingerprint != expected:
            raise SourceChangedError

    @staticmethod
    def _source_guard_record(
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
    def _require_source_guard(
        cls,
        recorded: CredentialSourceGuardRecord | None,
        guard: CredentialSourceGuard | None,
    ) -> None:
        if recorded is None:
            if guard is not None:
                raise SourceChangedError
            return
        if guard is None or cls._source_guard_record(guard) != recorded:
            raise SourceChangedError
        observed = guard.reader()
        if guard.expected is AuthorityExpectation.ABSENT:
            if observed is not None:
                raise SourceChangedError
        elif observed is None or observed.fingerprint != guard.expected:
            raise SourceChangedError

    def _write_journal(
        self,
        journal: CredentialTransactionJournal,
    ) -> None:
        self._tree.ensure_transaction_directory()
        if self._tree.transaction_artifacts_present():
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        self._tree.write_owned_file(
            self._tree.transaction_directory,
            PRIVATE_TRANSACTION_JOURNAL,
            encode_credential_journal(journal),
            expected_source=AuthorityExpectation.ABSENT,
        )

    def _materialize_private_candidates(
        self,
        plan: _TransactionPlan,
    ) -> None:
        directory = self._tree.transaction_directory
        for planned in plan.files:
            self._tree.write_owned_file(
                directory,
                planned.record.stage_basename,
                planned.target,
                expected_source=AuthorityExpectation.ABSENT,
            )
        for planned in plan.files:
            if (
                planned.base is not None
                and planned.record.backup_basename is not None
            ):
                self._tree.write_owned_file(
                    directory,
                    planned.record.backup_basename,
                    planned.base.data,
                    expected_source=AuthorityExpectation.ABSENT,
                )

    def _apply_target(self, plan: _TransactionPlan) -> None:
        journal = plan.journal
        for planned in plan.files:
            bundle = self._tree.root / planned.record.bundle_basename
            stage = self._required_transaction_file(
                planned.record.stage_basename,
                Sha256Digest(planned.record.target_sha256),
            )
            self._tree.write_owned_file(
                bundle,
                planned.record.basename,
                stage.data,
                expected_source=_expected_source(planned.base),
            )
        self._require_target_files(journal)

    def _restore_base(self, journal: CredentialTransactionJournal) -> None:
        for record in journal.files:
            bundle = self._tree.root / record.bundle_basename
            current = self._tree.read_owned_file(bundle, record.basename)
            target_digest = Sha256Digest(record.target_sha256)
            base_digest = (
                Sha256Digest(record.base_sha256)
                if record.base_sha256 is not None
                else None
            )
            if base_digest is None:
                if current is None:
                    continue
                if current.fingerprint.digest != target_digest:
                    raise SourceChangedError
                self._tree.delete_owned_file(
                    bundle,
                    record.basename,
                    current.fingerprint,
                )
                continue
            if (
                current is not None
                and current.fingerprint.digest == base_digest
            ):
                continue
            if current is None or current.fingerprint.digest != target_digest:
                raise SourceChangedError
            if record.backup_basename is None:
                raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
            backup = self._required_transaction_file(
                record.backup_basename,
                base_digest,
            )
            self._tree.write_owned_file(
                bundle,
                record.basename,
                backup.data,
                expected_source=current.fingerprint,
            )
        for basename in sorted(
            set(journal.target_bundles) - set(journal.base_present_bundles)
        ):
            bundle = self._tree.root / basename
            if self._tree.bundle_present(bundle):
                raise SourceChangedError
            self._tree.destroy_owned_directory(bundle)

    def _ensure_target(self, journal: CredentialTransactionJournal) -> None:
        for record in journal.files:
            bundle = self._tree.root / record.bundle_basename
            current = self._tree.read_owned_file(bundle, record.basename)
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
            self._tree.write_owned_file(
                bundle,
                record.basename,
                stage.data,
                expected_source=_expected_source(current),
            )
        self._require_target_files(journal)

    def _require_target_files(
        self,
        journal: CredentialTransactionJournal,
    ) -> None:
        for record in journal.files:
            bundle = self._tree.root / record.bundle_basename
            current = self._tree.read_owned_file(bundle, record.basename)
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

    def _delete_displaced(
        self,
        journal: CredentialTransactionJournal,
    ) -> None:
        for basename in journal.displaced_bundles:
            self._tree.destroy_owned_directory(self._tree.root / basename)

    def _cleanup_transaction(
        self,
        journal: CredentialTransactionJournal,
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
        self._tree.destroy_owned_directory(directory)

    @staticmethod
    def _authority_state(
        journal: CredentialTransactionJournal,
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


__all__ = ["CredentialSourceGuard", "PrivateCredentialTransaction"]
