"""Mutable storage for authoritative account token activity."""

from pathlib import Path

from sidekick_usages.core.models import AccountTokenActivitySnapshot
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
)
from sidekick_usages.persistence.snapshots.activity.reader import (
    ActivitySnapshotReader,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)


class ActivitySnapshotStore(ActivitySnapshotReader):
    """Persist last successful account activity under stable identity."""

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
        return self.save_many((snapshot,))[0]

    def save_many(
        self,
        snapshots: tuple[AccountTokenActivitySnapshot, ...],
    ) -> tuple[AccountTokenActivitySnapshot, ...]:
        """Merge observations through one decode and at most one commit."""
        if not snapshots:
            return ()
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    document = ActivitySnapshotDocument(
                        schema_version=ACTIVITY_SCHEMA_VERSION,
                        accounts={},
                    )
                    expected = AuthorityExpectation.ABSENT
                else:
                    document = decode_activity_document(observed.data)
                    expected = observed.fingerprint
                accounts = dict(document.accounts)
                effective: list[AccountTokenActivitySnapshot] = []
                for snapshot in snapshots:
                    effective.append(
                        merge_activity_snapshot(accounts, snapshot)
                    )
                payload = encode_activity_snapshot_document(
                    ActivitySnapshotDocument(
                        schema_version=ACTIVITY_SCHEMA_VERSION,
                        accounts=accounts,
                    )
                )
                if observed is not None and observed.data == payload:
                    return tuple(effective)
                self._filesystem.commit_opaque_private(
                    payload,
                    expected_source=expected,
                )
                return tuple(effective)
        except ActivitySnapshotError:
            raise
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.WRITE
            ) from None
