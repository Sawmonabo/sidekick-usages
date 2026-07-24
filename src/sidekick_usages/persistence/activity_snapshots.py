"""Durable storage for authoritative account token activity."""

from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    AccountTokenActivitySnapshot,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    ActivitySnapshotError,
    PersistenceError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.activity import (
    ACTIVITY_SCHEMA_VERSION,
    ActivitySnapshotDecodeError,
    ActivitySnapshotDocument,
    account_activity_snapshot,
    activity_record,
    decode_activity_snapshot_document,
    encode_activity_snapshot_document,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)


def _decode(payload: bytes) -> ActivitySnapshotDocument:
    try:
        return decode_activity_snapshot_document(payload)
    except ActivitySnapshotDecodeError:
        raise ActivitySnapshotError(
            ActivitySnapshotFailureKind.MALFORMED
        ) from None


def _identity_key(provider_id: ProviderId, account_id: str) -> Sha256Digest:
    try:
        value = f"{provider_id.value}\0{account_id}".encode()
    except UnicodeEncodeError:
        raise ValueError(
            "Provider account identity must be valid UTF-8."
        ) from None
    return sha256_digest(value)


def _merge(
    current: AccountTokenActivitySnapshot | None,
    incoming: AccountTokenActivitySnapshot,
) -> AccountTokenActivitySnapshot:
    if current is None:
        return incoming
    if incoming.fetched_at < current.fetched_at:
        return current
    if incoming.fetched_at == current.fetched_at:
        if incoming == current:
            return current
        raise ActivitySnapshotError(ActivitySnapshotFailureKind.CONFLICT)
    summary = incoming.summary
    if (
        summary.since is None
        and summary.total_tokens >= current.summary.total_tokens
    ):
        summary = replace(summary, since=current.summary.since)
    return replace(incoming, summary=summary)


class ActivitySnapshotStore:
    """Persist last successful account activity under stable identity."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Activity snapshot path must be absolute.")
        self.path = path
        self._filesystem = PersistenceFilesystem(path)
        self._lock = PersistenceLock(self._filesystem)

    @staticmethod
    def _account_id(account: Account) -> str | None:
        if account.provider_id is not ProviderId.CODEX:
            return None
        return account.provider_account_id

    def load(self, account: Account) -> AccountTokenActivitySnapshot | None:
        """Load one exact account snapshot without mutation."""
        if (account_id := self._account_id(account)) is None:
            return None
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.READ
            ) from None
        if observed is None:
            return None
        document = _decode(observed.data)
        record = document.accounts.get(
            str(_identity_key(account.provider_id, account_id))
        )
        return (
            None
            if record is None
            else account_activity_snapshot(account_id, record)
        )

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        """Merge and durably commit one authoritative account snapshot."""
        key = str(
            _identity_key(
                snapshot.provider_id,
                snapshot.provider_account_id,
            )
        )
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
                    document = _decode(observed.data)
                    expected = observed.fingerprint
                current_record = document.accounts.get(key)
                current = (
                    None
                    if current_record is None
                    else account_activity_snapshot(
                        snapshot.provider_account_id,
                        current_record,
                    )
                )
                effective = _merge(current, snapshot)
                updated = ActivitySnapshotDocument(
                    schema_version=ACTIVITY_SCHEMA_VERSION,
                    accounts={
                        **document.accounts,
                        key: activity_record(effective),
                    },
                )
                payload = encode_activity_snapshot_document(updated)
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
