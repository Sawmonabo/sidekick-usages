"""Passive activity snapshot reader."""

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    ProviderTokenActivitySnapshot,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    ActivitySnapshotError,
    PersistenceError,
)
from sidekick_usages.persistence.filesystem.reader import PrivateDocumentReader
from sidekick_usages.persistence.schema.activity import (
    ActivitySnapshotDocument,
)
from sidekick_usages.persistence.snapshots.activity.codec import (
    activity_snapshot_for_account,
    activity_snapshot_for_provider,
    decode_activity_document,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)

ACTIVITY_SNAPSHOT_PATH_ERROR = "Activity snapshot path must be absolute."


class ActivitySnapshotReader(PrivateDocumentReader):
    """Read cached activity without mutable coordination."""

    absolute_path_error = ACTIVITY_SNAPSHOT_PATH_ERROR

    @staticmethod
    def _account_id(account: SavedAccount) -> str | None:
        if account.provider_id is not ProviderId.CODEX:
            return None
        identity = account.provider_identity
        return None if identity is None else str(identity)

    def load(
        self,
        account: SavedAccount,
    ) -> AccountTokenActivitySnapshot | None:
        """Load one exact account snapshot without mutation."""
        return self.load_many((account,)).get(account.account_id)

    def load_all(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> tuple[
        tuple[
            tuple[
                SidekickAccountId,
                AccountTokenActivitySnapshot,
            ],
            ...,
        ],
        tuple[ProviderTokenActivitySnapshot, ...],
    ]:
        """Bulk-read account and provider activity through one decode."""
        document = self._load_document()
        if document is None:
            return (), ()
        snapshots: list[
            tuple[
                SidekickAccountId,
                AccountTokenActivitySnapshot,
            ]
        ] = []
        for account in accounts:
            provider_account_id = self._account_id(account)
            snapshot = (
                None
                if provider_account_id is None
                else activity_snapshot_for_account(
                    document,
                    account,
                    provider_account_id,
                )
            )
            if snapshot is not None:
                snapshots.append((account.account_id, snapshot))
        providers = tuple(
            snapshot
            for provider_id in ProviderId
            if (
                snapshot := activity_snapshot_for_provider(
                    document,
                    provider_id,
                )
            )
            is not None
        )
        return tuple(snapshots), providers

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountTokenActivitySnapshot]:
        """Load exact account snapshots through one document decode."""
        snapshots, _providers = self.load_all(accounts)
        return dict(snapshots)

    def _load_document(self) -> ActivitySnapshotDocument | None:
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.READ
            ) from None
        return (
            None
            if observed is None
            else decode_activity_document(observed.data)
        )
