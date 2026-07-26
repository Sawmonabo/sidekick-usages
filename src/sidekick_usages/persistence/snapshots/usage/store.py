"""Mutable last-successful usage for stable saved accounts."""

from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.models import AccountUsageSnapshot
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    PersistenceError,
    UsageSnapshotError,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.usage import (
    account_usage_snapshot,
    encode_usage_snapshot_document,
    usage_record,
)
from sidekick_usages.persistence.snapshots.usage.codec import (
    begin_usage_promotions,
    decode_usage_document,
    merge_usage_snapshot,
    promotion_identities,
    usage_document,
    without_usage_promotion,
)
from sidekick_usages.persistence.snapshots.usage.reader import (
    UsageSnapshotReader,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.error import UsageSnapshotFailureKind


class UsageSnapshotStore(UsageSnapshotReader):
    """Persist last successful usage under stable Sidekick account IDs."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._lock = PersistenceLock(self._filesystem)

    def save(
        self,
        snapshot: AccountUsageSnapshot,
    ) -> AccountUsageSnapshot:
        """Merge and durably commit one successful usage snapshot."""
        return self.save_many((snapshot,))[0]

    def save_many(
        self,
        snapshots: tuple[AccountUsageSnapshot, ...],
    ) -> tuple[AccountUsageSnapshot, ...]:
        """Merge observations through one decode and at most one commit."""
        if not snapshots:
            return ()
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    document = usage_document({}, {})
                    expected = AuthorityExpectation.ABSENT
                else:
                    document = decode_usage_document(observed.data)
                    expected = observed.fingerprint
                accounts = dict(document.accounts)
                promotions = dict(document.identity_promotions)
                effective: list[AccountUsageSnapshot] = []
                for snapshot in snapshots:
                    effective.append(
                        merge_usage_snapshot(
                            accounts,
                            promotions,
                            snapshot,
                        )
                    )
                payload = encode_usage_snapshot_document(
                    usage_document(accounts, promotions)
                )
                if observed is not None and observed.data == payload:
                    return tuple(effective)
                self._filesystem.commit_opaque_private(
                    payload,
                    expected_source=expected,
                )
                return tuple(effective)
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None

    def begin_identity_promotion(
        self,
        account_id: SidekickAccountId,
        provider_id: ProviderId,
        provider_identity: ProviderIdentity,
    ) -> None:
        """Record identity promotion before account authority commit."""
        key = str(account_id)
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    return
                document = decode_usage_document(observed.data)
                record = document.accounts.get(key)
                if record is None:
                    return
                current = account_usage_snapshot(account_id, record)
                if current.provider_id is not provider_id or (
                    current.provider_identity is not None
                    and current.provider_identity != provider_identity
                ):
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                promotions = begin_usage_promotions(
                    document,
                    key,
                    provider_id,
                    current.provider_identity,
                    provider_identity,
                )
                if promotions == document.identity_promotions:
                    return
                self._filesystem.commit_opaque_private(
                    encode_usage_snapshot_document(
                        usage_document(dict(document.accounts), promotions)
                    ),
                    expected_source=observed.fingerprint,
                )
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None

    def abort_identity_promotion(
        self,
        account_id: SidekickAccountId,
        provider_id: ProviderId,
        provider_identity: ProviderIdentity,
    ) -> None:
        """Restore one staged usage identity after a proven failed commit."""
        key = str(account_id)
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    return
                document = decode_usage_document(observed.data)
                pending = document.identity_promotions.get(key)
                if pending is None:
                    return
                source_identity, target_identity = promotion_identities(
                    pending
                )
                if (
                    ProviderId(pending.provider_id) is not provider_id
                    or target_identity != provider_identity
                ):
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                record = document.accounts[key]
                current = account_usage_snapshot(account_id, record)
                updated = replace(
                    current,
                    provider_identity=source_identity,
                )
                self._filesystem.commit_opaque_private(
                    encode_usage_snapshot_document(
                        usage_document(
                            {
                                **document.accounts,
                                key: usage_record(updated),
                            },
                            without_usage_promotion(document, key),
                        )
                    ),
                    expected_source=observed.fingerprint,
                )
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None

    def promote_identity(
        self,
        account_id: SidekickAccountId,
        provider_id: ProviderId,
        provider_identity: ProviderIdentity,
    ) -> None:
        """Bind unverified usage to one verified account identity."""
        key = str(account_id)
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    return
                document = decode_usage_document(observed.data)
                record = document.accounts.get(key)
                if record is None:
                    return
                current = account_usage_snapshot(account_id, record)
                pending = document.identity_promotions.get(key)
                if current.provider_id is not provider_id:
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                if pending is not None:
                    source_identity, target_identity = promotion_identities(
                        pending
                    )
                    if (
                        ProviderId(pending.provider_id) is not provider_id
                        or target_identity != provider_identity
                        or current.provider_identity
                        not in {source_identity, target_identity}
                    ):
                        raise UsageSnapshotError(
                            UsageSnapshotFailureKind.CONFLICT
                        )
                elif (
                    current.provider_identity is not None
                    and current.provider_identity != provider_identity
                ):
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                if (
                    current.provider_identity == provider_identity
                    and pending is None
                ):
                    return
                updated = usage_document(
                    {
                        **document.accounts,
                        key: usage_record(
                            replace(
                                current,
                                provider_identity=provider_identity,
                            )
                        ),
                    },
                    without_usage_promotion(document, key),
                )
                self._filesystem.commit_opaque_private(
                    encode_usage_snapshot_document(updated),
                    expected_source=observed.fingerprint,
                )
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None

    def pending_identity_promotions(
        self,
        provider_id: ProviderId,
    ) -> tuple[SidekickAccountId, ...]:
        """Return stable IDs with durable promotion intent for one provider."""
        document = self._load_document()
        if document is None:
            return ()
        return tuple(
            SidekickAccountId(account_id)
            for account_id, promotion in document.identity_promotions.items()
            if ProviderId(promotion.provider_id) is provider_id
        )

    def recover_identity_promotion(
        self,
        account_id: SidekickAccountId,
        account: SavedAccount | None,
    ) -> None:
        """Resolve one intent from the exact current saved-account identity."""
        key = str(account_id)
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    return
                document = decode_usage_document(observed.data)
                pending = document.identity_promotions.get(key)
                if pending is None:
                    return
                source_identity, target_identity = promotion_identities(
                    pending
                )
                provider_id = ProviderId(pending.provider_id)
                if account is None:
                    resolved_identity = source_identity
                elif (
                    account.account_id != account_id
                    or account.provider_id is not provider_id
                    or account.provider_identity
                    not in {source_identity, target_identity}
                ):
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                elif not account.has_managed_authority:
                    resolved_identity = source_identity
                else:
                    resolved_identity = account.provider_identity
                current = account_usage_snapshot(
                    account_id,
                    document.accounts[key],
                )
                updated = replace(
                    current,
                    provider_identity=resolved_identity,
                )
                self._filesystem.commit_opaque_private(
                    encode_usage_snapshot_document(
                        usage_document(
                            {
                                **document.accounts,
                                key: usage_record(updated),
                            },
                            without_usage_promotion(document, key),
                        )
                    ),
                    expected_source=observed.fingerprint,
                )
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None
