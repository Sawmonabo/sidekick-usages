"""Durable last-successful usage for stable saved accounts."""

from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.models import AccountUsageSnapshot
from sidekick_usages.persistence.errors import (
    PersistenceError,
    UsageSnapshotError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.usage import (
    USAGE_SCHEMA_VERSION,
    UsageSnapshotDecodeError,
    UsageSnapshotDocument,
    account_usage_snapshot,
    decode_usage_snapshot_document,
    encode_usage_snapshot_document,
    usage_record,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.error import UsageSnapshotFailureKind


def _decode(payload: bytes) -> UsageSnapshotDocument:
    try:
        return decode_usage_snapshot_document(payload)
    except UsageSnapshotDecodeError:
        raise UsageSnapshotError(UsageSnapshotFailureKind.MALFORMED) from None


def _merge(
    current: AccountUsageSnapshot | None,
    incoming: AccountUsageSnapshot,
) -> AccountUsageSnapshot:
    if current is not None and (
        current.account_id != incoming.account_id
        or current.provider_id is not incoming.provider_id
        or current.provider_identity != incoming.provider_identity
    ):
        raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
    if current is None or incoming.fetched_at > current.fetched_at:
        return incoming
    if incoming.fetched_at < current.fetched_at or incoming == current:
        return current
    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)


class UsageSnapshotStore:
    """Persist last successful usage under stable Sidekick account IDs."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Usage snapshot path must be absolute.")
        self.path = path
        self._filesystem = PersistenceFilesystem(path)
        self._lock = PersistenceLock(self._filesystem)

    def load(self, account: SavedAccount) -> AccountUsageSnapshot | None:
        """Load one exact account snapshot without mutation."""
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.READ) from None
        if observed is None:
            return None
        document = _decode(observed.data)
        record = document.accounts.get(str(account.account_id))
        if record is None:
            return None
        snapshot = account_usage_snapshot(account.account_id, record)
        if (
            snapshot.provider_id is not account.provider_id
            or snapshot.provider_identity != account.provider_identity
        ):
            raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
        return snapshot

    def save(
        self,
        snapshot: AccountUsageSnapshot,
    ) -> AccountUsageSnapshot:
        """Merge and durably commit one successful usage snapshot."""
        key = str(snapshot.account_id)
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    document = UsageSnapshotDocument(
                        schema_version=USAGE_SCHEMA_VERSION,
                        accounts={},
                    )
                    expected = AuthorityExpectation.ABSENT
                else:
                    document = _decode(observed.data)
                    expected = observed.fingerprint
                current_record = document.accounts.get(key)
                current = (
                    None
                    if current_record is None
                    else account_usage_snapshot(
                        snapshot.account_id,
                        current_record,
                    )
                )
                effective = _merge(current, snapshot)
                updated = UsageSnapshotDocument(
                    schema_version=USAGE_SCHEMA_VERSION,
                    accounts={
                        **document.accounts,
                        key: usage_record(effective),
                    },
                )
                payload = encode_usage_snapshot_document(updated)
                if observed is not None and observed.data == payload:
                    return effective
                self._filesystem.commit_opaque_private(
                    payload,
                    expected_source=expected,
                )
                return effective
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None
