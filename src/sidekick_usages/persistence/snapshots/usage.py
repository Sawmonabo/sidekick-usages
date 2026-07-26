"""Durable last-successful usage for stable saved accounts."""

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
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.usage import (
    USAGE_SCHEMA_VERSION,
    UsageIdentityPromotionRecord,
    UsageSnapshotDecodeError,
    UsageSnapshotDocument,
    UsageSnapshotRecord,
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


def _document(
    accounts: dict[str, UsageSnapshotRecord],
    promotions: dict[str, UsageIdentityPromotionRecord],
) -> UsageSnapshotDocument:
    """Build one current strict usage document."""
    return UsageSnapshotDocument(
        schema_version=USAGE_SCHEMA_VERSION,
        accounts=accounts,
        identity_promotions=promotions,
    )


def _promotion(
    provider_id: ProviderId,
    source_identity: ProviderIdentity | None,
    target_identity: ProviderIdentity,
) -> UsageIdentityPromotionRecord:
    """Build one secret-free exact identity-promotion intent."""
    return UsageIdentityPromotionRecord(
        provider_id=provider_id.value,
        source_identity=(
            None if source_identity is None else str(source_identity)
        ),
        target_identity=str(target_identity),
    )


def _promotion_identities(
    promotion: UsageIdentityPromotionRecord,
) -> tuple[ProviderIdentity | None, ProviderIdentity]:
    """Return one promotion's exact source and target identities."""
    source = (
        None
        if promotion.source_identity is None
        else ProviderIdentity(promotion.source_identity)
    )
    return source, ProviderIdentity(promotion.target_identity)


def _without_promotion(
    document: UsageSnapshotDocument,
    key: str,
) -> dict[str, UsageIdentityPromotionRecord]:
    """Return the current promotions except one completed account."""
    return {
        account_id: promotion
        for account_id, promotion in document.identity_promotions.items()
        if account_id != key
    }


def _matches_promotion(
    promotion: UsageIdentityPromotionRecord,
    provider_id: ProviderId,
    provider_identity: ProviderIdentity,
    current_identity: ProviderIdentity | None,
) -> bool:
    source_identity, target_identity = _promotion_identities(promotion)
    return (
        ProviderId(promotion.provider_id) is provider_id
        and target_identity == provider_identity
        and current_identity in {source_identity, target_identity}
    )


def _snapshot(
    document: UsageSnapshotDocument,
    account: SavedAccount,
) -> AccountUsageSnapshot | None:
    """Read one exact account snapshot from an already-decoded document."""
    key = str(account.account_id)
    record = document.accounts.get(key)
    if record is None:
        return None
    snapshot = account_usage_snapshot(account.account_id, record)
    if snapshot.provider_id is not account.provider_id:
        raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
    promotion = document.identity_promotions.get(key)
    if promotion is None:
        if snapshot.provider_identity != account.provider_identity:
            raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
        return snapshot
    source_identity, target_identity = _promotion_identities(promotion)
    if ProviderId(
        promotion.provider_id
    ) is not account.provider_id or account.provider_identity not in {
        source_identity,
        target_identity,
    }:
        raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
    return replace(
        snapshot,
        provider_identity=account.provider_identity,
    )


def _merge_snapshot(
    accounts: dict[str, UsageSnapshotRecord],
    promotions: dict[str, UsageIdentityPromotionRecord],
    snapshot: AccountUsageSnapshot,
) -> AccountUsageSnapshot:
    """Merge one observation into an already-decoded usage document."""
    key = str(snapshot.account_id)
    current_record = accounts.get(key)
    current = (
        None
        if current_record is None
        else account_usage_snapshot(
            snapshot.account_id,
            current_record,
        )
    )
    pending = promotions.get(key)
    incoming = snapshot
    if pending is not None:
        source_identity, target_identity = _promotion_identities(pending)
        if incoming.provider_id is not ProviderId(
            pending.provider_id
        ) or incoming.provider_identity not in {
            source_identity,
            target_identity,
        }:
            raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
        if (
            current is not None
            and current.provider_identity == target_identity
            and incoming.provider_identity == source_identity
        ):
            incoming = replace(
                incoming,
                provider_identity=target_identity,
            )
        elif incoming.provider_identity == target_identity:
            if current is not None:
                current = replace(
                    current,
                    provider_identity=target_identity,
                )
            promotions.pop(key)
    effective = _merge(current, incoming)
    accounts[key] = usage_record(effective)
    return effective


def _begun_promotions(
    document: UsageSnapshotDocument,
    key: str,
    provider_id: ProviderId,
    current_identity: ProviderIdentity | None,
    target_identity: ProviderIdentity,
) -> dict[str, UsageIdentityPromotionRecord]:
    promotions = dict(document.identity_promotions)
    pending = promotions.get(key)
    if current_identity == target_identity:
        if pending is None:
            return promotions
        if not _matches_promotion(
            pending,
            provider_id,
            target_identity,
            current_identity,
        ):
            raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
        promotions.pop(key)
        return promotions
    expected = _promotion(
        provider_id,
        current_identity,
        target_identity,
    )
    if pending is None:
        promotions[key] = expected
    elif pending != expected:
        raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
    return promotions


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
                snapshot = _snapshot(document, account)
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
        return None if observed is None else _decode(observed.data)

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
                    document = _document({}, {})
                    expected = AuthorityExpectation.ABSENT
                else:
                    document = _decode(observed.data)
                    expected = observed.fingerprint
                accounts = dict(document.accounts)
                promotions = dict(document.identity_promotions)
                effective: list[AccountUsageSnapshot] = []
                for snapshot in snapshots:
                    effective.append(
                        _merge_snapshot(
                            accounts,
                            promotions,
                            snapshot,
                        )
                    )
                payload = encode_usage_snapshot_document(
                    _document(accounts, promotions)
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
                document = _decode(observed.data)
                record = document.accounts.get(key)
                if record is None:
                    return
                current = account_usage_snapshot(account_id, record)
                if current.provider_id is not provider_id or (
                    current.provider_identity is not None
                    and current.provider_identity != provider_identity
                ):
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                promotions = _begun_promotions(
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
                        _document(dict(document.accounts), promotions)
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
                document = _decode(observed.data)
                pending = document.identity_promotions.get(key)
                if pending is None:
                    return
                source_identity, target_identity = _promotion_identities(
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
                        _document(
                            {
                                **document.accounts,
                                key: usage_record(updated),
                            },
                            _without_promotion(document, key),
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
                document = _decode(observed.data)
                record = document.accounts.get(key)
                if record is None:
                    return
                current = account_usage_snapshot(account_id, record)
                pending = document.identity_promotions.get(key)
                if current.provider_id is not provider_id:
                    raise UsageSnapshotError(UsageSnapshotFailureKind.CONFLICT)
                if pending is not None:
                    source_identity, target_identity = _promotion_identities(
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
                updated = _document(
                    {
                        **document.accounts,
                        key: usage_record(
                            replace(
                                current,
                                provider_identity=provider_identity,
                            )
                        ),
                    },
                    _without_promotion(document, key),
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
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.READ) from None
        if observed is None:
            return ()
        document = _decode(observed.data)
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
                document = _decode(observed.data)
                pending = document.identity_promotions.get(key)
                if pending is None:
                    return
                source_identity, target_identity = _promotion_identities(
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
                        _document(
                            {
                                **document.accounts,
                                key: usage_record(updated),
                            },
                            _without_promotion(document, key),
                        )
                    ),
                    expected_source=observed.fingerprint,
                )
        except UsageSnapshotError:
            raise
        except PersistenceError:
            raise UsageSnapshotError(UsageSnapshotFailureKind.WRITE) from None
