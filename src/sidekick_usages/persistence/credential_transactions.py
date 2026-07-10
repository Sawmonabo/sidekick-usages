"""Crash-recoverable account-authority and private-bundle coordination."""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
    Sha256Digest,
)
from sidekick_usages.persistence.credential_transaction_plans import (
    CredentialTransactionPlan,
    build_migration_transaction_plan,
    build_runtime_transaction_plan,
    validate_migration_displaced,
    validate_migration_generations,
    validate_runtime_displaced,
)
from sidekick_usages.persistence.credential_transaction_recovery import (
    CredentialSourceGuard,
    CredentialTransactionRecovery,
    DivergentSourceOutcome,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    CredentialJournal,
    encode_credential_journal,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    SourceChangedError,
)
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)


class _AuthorityCommitter(Protocol):
    """Held-lock authority operation used at the final commit point."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact authoritative bytes."""


class _LineagePublisher(Protocol):
    """Held-lock capability for content-addressed lineage publication."""

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish or reuse one exact content-addressed lineage snapshot."""


class _MigrationTransaction(
    _AuthorityCommitter,
    _LineagePublisher,
    Protocol,
):
    """Held-lock migration commit and lineage publication capability."""


type _AuthorityReader = Callable[[], FileSnapshot | None]


class PrivateCredentialTransaction:
    """Coordinate private bytes and one version-one authority under a lock."""

    def __init__(
        self,
        tree: PrivateCredentialTree,
        authority_reader: _AuthorityReader,
    ) -> None:
        self._tree = tree
        self._authority_reader = authority_reader
        self._recovery = CredentialTransactionRecovery(
            tree,
            authority_reader,
        )

    def recover(
        self,
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> bool:
        """Resolve one interrupted transaction from fresh filesystem state.

        :returns: Whether transaction evidence was found and resolved.
        """
        return self._recovery.recover(source_guard=source_guard)

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
        return self._recovery.recover_migration(
            transaction,
            source_guard=source_guard,
        )

    def commit(
        self,
        transaction: _AuthorityCommitter,
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
        displaced = validate_runtime_displaced(
            self._tree,
            displaced_bundles,
        )
        if not private_bundles and not displaced and source_guard is None:
            return transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                payload,
                expected_source,
            )
        self._require_authority(expected_source)
        self._recovery.require_source_guard(
            self._recovery.source_guard_record(source_guard),
            source_guard,
        )
        plan = build_runtime_transaction_plan(
            self._tree,
            payload,
            expected_source,
            private_bundles,
            displaced,
            self._recovery.source_guard_record(source_guard),
        )
        self._write_journal(plan.journal)
        try:
            self._materialize_private_candidates(plan)
            self._recovery.apply_target(plan)
            self._recovery.require_source_guard(
                plan.journal.source_guard,
                source_guard,
            )
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
            self._recovery.require_source_guard(
                plan.journal.source_guard,
                source_guard,
            )
            self._recovery.delete_displaced(plan.journal)
            self._recovery.cleanup_transaction(plan.journal)
            return final
        except Exception:
            self.recover(source_guard=source_guard)
            raise

    def commit_migration(
        self,
        transaction: _MigrationTransaction,
        target_generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
        *,
        base_generation: AuthorityGeneration | None,
        private_bundles: tuple[PreparedPrivateBundleWrite, ...],
        displaced_bundles: Iterable[Path],
        source_guard: CredentialSourceGuard,
    ) -> FileSnapshot:
        """Commit one migration generation through a version-two journal.

        :param transaction: Capability proving the canonical lock is held.
        :param target_generation: Validated canonical target generation.
        :param payload: Exact canonical target authority bytes.
        :param expected_source: Exact old canonical authority expectation.
        :param base_generation: Old generation, or ``None`` when absent.
        :param private_bundles: Bounded nested private target mutations.
        :param displaced_bundles: Canonical homes no longer referenced.
        :param source_guard: Distinct compatibility authority retained.
        :returns: Reopened and verified target authority.
        """
        if self._tree.transaction_directory_present():
            raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
        validate_migration_generations(
            expected_source,
            base_generation,
            target_generation,
            payload,
        )
        displaced = validate_migration_displaced(
            self._tree,
            displaced_bundles,
        )
        self._require_authority(expected_source)
        recorded_guard = self._recovery.source_guard_record(source_guard)
        if recorded_guard is None:
            raise ValueError("Migration source guard is required.")
        self._recovery.require_source_guard(recorded_guard, source_guard)
        plan = build_migration_transaction_plan(
            self._tree,
            payload,
            expected_source,
            base_generation,
            target_generation,
            private_bundles,
            displaced,
            recorded_guard,
        )
        self._write_journal(plan.journal)
        self._materialize_private_candidates(plan)
        self._recovery.apply_target(plan)
        self._recovery.require_source_guard(
            plan.journal.source_guard,
            source_guard,
        )
        final = transaction.commit_authority(
            target_generation,
            payload,
            expected_source,
        )
        if (
            final.fingerprint.digest
            != Sha256Digest(plan.journal.target_authority_sha256)
            or final.fingerprint.size != plan.journal.target_authority_size
        ):
            raise SourceChangedError
        self._recovery.require_source_guard(
            plan.journal.source_guard,
            source_guard,
        )
        self._recovery.delete_displaced(plan.journal)
        self._recovery.require_target_state(plan.journal)
        transaction.publish_immutable(target_generation, final)
        self._recovery.require_source_guard(
            plan.journal.source_guard,
            source_guard,
        )
        self._recovery.require_target_state(plan.journal)
        self._recovery.cleanup_transaction(plan.journal)
        return final

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
        return self._recovery.resolve_migration_source_divergence(
            transaction,
            source_guard=source_guard,
        )

    def _require_authority(self, expected: ExpectedAuthority) -> None:
        current = self._authority_reader()
        if expected is AuthorityExpectation.ABSENT:
            if current is not None:
                raise SourceChangedError
        elif current is None or current.fingerprint != expected:
            raise SourceChangedError

    def _write_journal(
        self,
        journal: CredentialJournal,
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
        plan: CredentialTransactionPlan,
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


__all__ = [
    "CredentialSourceGuard",
    "DivergentSourceOutcome",
    "PrivateCredentialTransaction",
]
