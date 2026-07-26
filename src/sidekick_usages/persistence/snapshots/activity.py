"""Durable storage for authoritative account token activity."""

from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    ActivitySnapshotError,
    PersistenceError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.activity import (
    ACTIVITY_SCHEMA_VERSION,
    ActivitySnapshotDecodeError,
    ActivitySnapshotDocument,
    ActivitySnapshotRecord,
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


def _snapshot(
    document: ActivitySnapshotDocument,
    account: SavedAccount,
    provider_account_id: str,
) -> AccountTokenActivitySnapshot | None:
    """Read one account snapshot from an already-decoded document."""
    record = document.accounts.get(
        str(_identity_key(account.provider_id, provider_account_id))
    )
    return (
        None
        if record is None
        else account_activity_snapshot(provider_account_id, record)
    )


def _merge_snapshot(
    accounts: dict[str, ActivitySnapshotRecord],
    snapshot: AccountTokenActivitySnapshot,
) -> AccountTokenActivitySnapshot:
    """Merge one observation into an already-decoded activity document."""
    key = str(
        _identity_key(
            snapshot.provider_id,
            snapshot.provider_account_id,
        )
    )
    current_record = accounts.get(key)
    current = (
        None
        if current_record is None
        else account_activity_snapshot(
            snapshot.provider_account_id,
            current_record,
        )
    )
    effective = _merge(current, snapshot)
    accounts[key] = activity_record(effective)
    return effective


class ActivitySnapshotStore:
    """Persist last successful account activity under stable identity."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Activity snapshot path must be absolute.")
        self.path = path
        self._filesystem = PersistenceFilesystem(path)
        self._lock = PersistenceLock(self._filesystem)

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
            SidekickAccountId,
            AccountTokenActivitySnapshot,
        ],
        ...,
    ]:
        """Bulk-read account activity and bind it to stable account IDs."""
        document = self._load_document()
        if document is None:
            return ()
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
                else _snapshot(document, account, provider_account_id)
            )
            if snapshot is not None:
                snapshots.append((account.account_id, snapshot))
        return tuple(snapshots)

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountTokenActivitySnapshot]:
        """Load exact account snapshots through one document decode."""
        return dict(self.load_all(accounts))

    def _load_document(self) -> ActivitySnapshotDocument | None:
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.READ
            ) from None
        return None if observed is None else _decode(observed.data)

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
                    document = _decode(observed.data)
                    expected = observed.fingerprint
                accounts = dict(document.accounts)
                effective: list[AccountTokenActivitySnapshot] = []
                for snapshot in snapshots:
                    effective.append(_merge_snapshot(accounts, snapshot))
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
