"""Passive usage snapshot reader."""

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import AccountUsageSnapshot
from sidekick_usages.persistence.errors import (
    PersistenceError,
    UsageSnapshotError,
)
from sidekick_usages.persistence.filesystem.reader import PrivateDocumentReader
from sidekick_usages.persistence.schema.usage import UsageSnapshotDocument
from sidekick_usages.persistence.snapshots.usage.codec import (
    decode_usage_document,
    usage_snapshot_for_account,
)
from sidekick_usages.persistence.types.error import UsageSnapshotFailureKind

USAGE_SNAPSHOT_PATH_ERROR = "Usage snapshot path must be absolute."


class UsageSnapshotReader(PrivateDocumentReader):
    """Read last-successful usage without mutable coordination."""

    absolute_path_error = USAGE_SNAPSHOT_PATH_ERROR

    def load(self, account: SavedAccount) -> AccountUsageSnapshot | None:
        """Load one exact account snapshot without mutation."""
        return self.load_many((account,)).get(account.account_id)

    def load_all(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> tuple[
        tuple[AccountUsageSnapshot, ...],
        tuple[SidekickAccountId, ...],
    ]:
        """Bulk-read cached usage and isolate account identity conflicts."""
        document = self._load_document()
        if document is None:
            return (), ()
        snapshots: list[AccountUsageSnapshot] = []
        conflicts: list[SidekickAccountId] = []
        for account in accounts:
            try:
                snapshot = usage_snapshot_for_account(document, account)
            except UsageSnapshotError as error:
                if error.kind is not UsageSnapshotFailureKind.CONFLICT:
                    raise
                conflicts.append(account.account_id)
                continue
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots), tuple(conflicts)

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountUsageSnapshot]:
        """Load exact account snapshots through one document decode."""
        snapshots, conflicts = self.load_all(accounts)
        if conflicts:
            raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
        return {snapshot.account_id: snapshot for snapshot in snapshots}

    def _load_document(self) -> UsageSnapshotDocument | None:
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.READ) from None
        return (
            None if observed is None else decode_usage_document(observed.data)
        )
