"""Mutable storage for account and provider token activity."""

from pathlib import Path

from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    ProviderTokenActivitySnapshot,
)
from sidekick_usages.persistence.errors import (
    ActivitySnapshotError,
    PersistenceError,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.schema.activity import (
    ACTIVITY_SCHEMA_VERSION,
    ActivitySnapshotDocument,
    encode_activity_snapshot_document,
)
from sidekick_usages.persistence.snapshots.activity.codec import (
    decode_activity_document,
    merge_activity_snapshot,
    merge_provider_activity_snapshot,
)
from sidekick_usages.persistence.snapshots.activity.reader import (
    ActivitySnapshotReader,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)


class ActivitySnapshotStore(ActivitySnapshotReader):
    """Persist account and provider activity in one document."""

    _filesystem: PrivateFilesystem

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            filesystem_factory=PrivateFilesystem,
        )
        self._lock = PersistenceLock(self._filesystem)

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        """Merge and durably commit one authoritative account snapshot."""
        accounts, _providers = self.save_many((snapshot,), ())
        return accounts[0]

    def save_many(
        self,
        accounts: tuple[AccountTokenActivitySnapshot, ...],
        providers: tuple[ProviderTokenActivitySnapshot, ...],
    ) -> tuple[
        tuple[AccountTokenActivitySnapshot, ...],
        tuple[ProviderTokenActivitySnapshot, ...],
    ]:
        """Merge observations through one decode and at most one commit."""
        if not accounts and not providers:
            return (), ()
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    document = ActivitySnapshotDocument(
                        schema_version=ACTIVITY_SCHEMA_VERSION,
                        accounts={},
                        providers={},
                    )
                    expected = AuthorityExpectation.ABSENT
                else:
                    expected = observed.fingerprint
                    try:
                        document = decode_activity_document(observed.data)
                    except ActivitySnapshotError as error:
                        if (
                            error.kind
                            is not ActivitySnapshotFailureKind.MALFORMED
                        ):
                            raise
                        document = ActivitySnapshotDocument(
                            schema_version=ACTIVITY_SCHEMA_VERSION,
                            accounts={},
                            providers={},
                        )
                account_records = dict(document.accounts)
                provider_records = dict(document.providers)
                effective_accounts = tuple(
                    merge_activity_snapshot(account_records, snapshot)
                    for snapshot in accounts
                )
                effective_providers = tuple(
                    merge_provider_activity_snapshot(
                        provider_records,
                        snapshot,
                    )
                    for snapshot in providers
                )
                payload = encode_activity_snapshot_document(
                    ActivitySnapshotDocument(
                        schema_version=ACTIVITY_SCHEMA_VERSION,
                        accounts=account_records,
                        providers=provider_records,
                    )
                )
                effective = (effective_accounts, effective_providers)
                if observed is not None and observed.data == payload:
                    return effective
                self._filesystem.commit_opaque_private(
                    payload,
                    expected_source=expected,
                )
                return effective
        except ActivitySnapshotError:
            raise
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.WRITE
            ) from None
