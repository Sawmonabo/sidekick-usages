"""Crash-recoverable account-authority and private-bundle coordination."""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.credential_transaction_plans import (
    CredentialTransactionPlan,
    build_runtime_transaction_plan,
    validate_runtime_displaced,
)
from sidekick_usages.persistence.credential_transaction_recovery import (
    CredentialSourceGuard,
    CredentialTransactionRecovery,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    SourceChangedError,
)
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.private_bundle_paths import (
    PRIVATE_TRANSACTION_JOURNAL,
)
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.transaction import (
    CredentialJournal,
    encode_credential_journal,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    Sha256Digest,
)


class _AuthorityCommitter(Protocol):
    """Held-lock authority operation used at the final commit point."""

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact authoritative bytes."""


type _AuthorityReader = Callable[[], FileSnapshot | None]


class PrivateCredentialTransaction:
    """Coordinate private bytes and one current authority under a lock."""

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
        """Commit private changes first and current authority last.

        :param transaction: Capability proving the account lock is held.
        :param payload: Canonical target current authority bytes.
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
    "PrivateCredentialTransaction",
]
