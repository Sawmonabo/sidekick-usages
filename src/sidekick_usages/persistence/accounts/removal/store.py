"""Atomic durable saved-account removal coordination."""

from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.accounts.removal.models import (
    AccountRemovalDocument,
    AccountRemovalPhase,
    AccountRemovalRecord,
)
from sidekick_usages.persistence.accounts.removal.schema import (
    decode_account_removals,
    encode_account_removals,
)
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.state.files import recover_state_file
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    Sha256Digest,
    sha256_digest,
)

_REMOVAL_STATE_BASENAME = "account-removals.json"


class AccountRemovalStore:
    """Persist one idempotent removal record per stable account ID."""

    def __init__(self, operations_root: Path) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        self.path = operations_root / _REMOVAL_STATE_BASENAME
        self._filesystem = ManagedStateFilesystem(
            self.path,
            decode_account_removals,
        )
        self._lock = PersistenceLock(self._filesystem)

    def load(self) -> tuple[AccountRemovalRecord, ...]:
        """Recover interrupted writes and return every durable record."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            return self._document(
                self._filesystem.read_opaque_private()
            ).records

    def get(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRemovalRecord | None:
        """Return one recovered stable-ID record when present."""
        return next(
            (
                record
                for record in self.load()
                if record.account_id == account_id
            ),
            None,
        )

    def prepare(
        self,
        account: SavedAccount,
        *,
        profile_retired: bool,
    ) -> AccountRemovalRecord:
        """Persist one exact saved-account removal intent."""
        record = AccountRemovalRecord(
            account_id=account.account_id,
            provider_id=account.provider_id,
            expected_account_digest=_account_digest(account),
            phase=(
                AccountRemovalPhase.PROFILE_RETIRED
                if profile_retired
                else AccountRemovalPhase.PREPARED
            ),
        )
        return self._insert(record)

    def prepare_orphan(
        self,
        account_id: SidekickAccountId,
        provider_id: ProviderId,
    ) -> AccountRemovalRecord:
        """Persist cleanup intent for one qualified orphan profile."""
        return self._insert(
            AccountRemovalRecord(
                account_id=account_id,
                provider_id=provider_id,
                expected_account_digest=None,
                phase=AccountRemovalPhase.METADATA_REMOVED,
            )
        )

    def matches(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount,
    ) -> bool:
        """Return whether current account metadata matches removal intent."""
        return (
            record.account_id == account.account_id
            and record.provider_id is account.provider_id
            and record.expected_account_digest == _account_digest(account)
        )

    def reauthorize(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount,
        *,
        profile_retired: bool,
    ) -> AccountRemovalRecord:
        """Bind an explicit retry to current pre-removal account state."""
        if (
            record.phase.metadata_removed
            or record.account_id != account.account_id
            or record.provider_id is not account.provider_id
        ):
            raise SourceChangedError
        candidate = AccountRemovalRecord(
            account_id=record.account_id,
            provider_id=record.provider_id,
            expected_account_digest=_account_digest(account),
            phase=(
                AccountRemovalPhase.PROFILE_RETIRED
                if profile_retired
                else AccountRemovalPhase.PREPARED
            ),
        )
        if candidate == record:
            return record
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            if document.get(record.account_id) != record:
                raise SourceChangedError
            records = tuple(
                candidate
                if current.account_id == record.account_id
                else current
                for current in document.records
            )
            self._commit(AccountRemovalDocument(records), snapshot)
        return candidate

    def mark_profile_retired(
        self,
        record: AccountRemovalRecord,
    ) -> AccountRemovalRecord:
        """Advance one exact record across provider-profile retirement."""
        if record.phase.profile_retired:
            return record
        phase = (
            AccountRemovalPhase.FINALIZING
            if record.phase is AccountRemovalPhase.METADATA_REMOVED
            else AccountRemovalPhase.PROFILE_RETIRED
        )
        return self._advance(record, phase)

    def mark_metadata_removed(
        self,
        record: AccountRemovalRecord,
    ) -> AccountRemovalRecord:
        """Advance one exact record across saved-metadata removal."""
        if record.phase.metadata_removed:
            return record
        phase = (
            AccountRemovalPhase.FINALIZING
            if record.phase is AccountRemovalPhase.PROFILE_RETIRED
            else AccountRemovalPhase.METADATA_REMOVED
        )
        return self._advance(record, phase)

    def delete(self, record: AccountRemovalRecord) -> None:
        """Delete one exact completed or canceled removal record."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            current = document.get(record.account_id)
            if current is None:
                return
            if current != record:
                raise SourceChangedError
            retained = tuple(
                current
                for current in document.records
                if current.account_id != record.account_id
            )
            self._commit(AccountRemovalDocument(retained), snapshot)

    def _insert(
        self,
        record: AccountRemovalRecord,
    ) -> AccountRemovalRecord:
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            current = document.get(record.account_id)
            if current is not None:
                if current == record:
                    return current
                raise SourceChangedError
            self._commit(
                AccountRemovalDocument((*document.records, record)),
                snapshot,
            )
        return record

    def _advance(
        self,
        record: AccountRemovalRecord,
        phase: AccountRemovalPhase,
    ) -> AccountRemovalRecord:
        candidate = AccountRemovalRecord(
            account_id=record.account_id,
            provider_id=record.provider_id,
            expected_account_digest=record.expected_account_digest,
            phase=phase,
        )
        if not _valid_transition(record.phase, phase):
            raise ValueError("Account removal phase transition is invalid.")
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = self._document(snapshot)
            current = document.get(record.account_id)
            if current == candidate:
                return candidate
            if current != record:
                raise SourceChangedError
            records = tuple(
                candidate
                if current.account_id == record.account_id
                else current
                for current in document.records
            )
            self._commit(AccountRemovalDocument(records), snapshot)
        return candidate

    def _commit(
        self,
        document: AccountRemovalDocument,
        snapshot: FileSnapshot | None,
    ) -> None:
        if not document.records:
            if snapshot is not None:
                self._filesystem.delete_opaque_private(snapshot.fingerprint)
            return
        payload = encode_account_removals(document)
        if snapshot is not None and snapshot.data == payload:
            return
        self._filesystem.commit_opaque_private(
            payload,
            expected_source=(
                AuthorityExpectation.ABSENT
                if snapshot is None
                else snapshot.fingerprint
            ),
        )

    @staticmethod
    def _document(
        snapshot: FileSnapshot | None,
    ) -> AccountRemovalDocument:
        return (
            AccountRemovalDocument()
            if snapshot is None
            else decode_account_removals(snapshot.data)
        )


def _account_digest(account: SavedAccount) -> Sha256Digest:
    payload = encode_version_three(VersionThreeDocument((account,)))
    return sha256_digest(payload)


def _valid_transition(
    current: AccountRemovalPhase,
    candidate: AccountRemovalPhase,
) -> bool:
    return (
        candidate
        in {
            AccountRemovalPhase.PREPARED: {
                AccountRemovalPhase.PROFILE_RETIRED,
                AccountRemovalPhase.METADATA_REMOVED,
            },
            AccountRemovalPhase.PROFILE_RETIRED: {
                AccountRemovalPhase.FINALIZING,
            },
            AccountRemovalPhase.METADATA_REMOVED: {
                AccountRemovalPhase.FINALIZING,
            },
            AccountRemovalPhase.FINALIZING: set(),
        }[current]
    )
