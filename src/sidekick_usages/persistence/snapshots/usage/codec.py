"""Shared usage snapshot validation and identity policy."""

from dataclasses import replace

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.core.models import AccountUsageSnapshot
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import UsageSnapshotError
from sidekick_usages.persistence.schema.usage import (
    USAGE_SCHEMA_VERSION,
    UsageIdentityPromotionRecord,
    UsageSnapshotDecodeError,
    UsageSnapshotDocument,
    UsageSnapshotRecord,
    account_usage_snapshot,
    decode_usage_snapshot_document,
    usage_record,
)
from sidekick_usages.persistence.types.error import UsageSnapshotFailureKind


def decode_usage_document(payload: bytes) -> UsageSnapshotDocument:
    """Decode one strict usage document with the typed error vocabulary."""
    try:
        return decode_usage_snapshot_document(payload)
    except UsageSnapshotDecodeError:
        raise UsageSnapshotError(UsageSnapshotFailureKind.MALFORMED) from None


def usage_document(
    accounts: dict[str, UsageSnapshotRecord],
    promotions: dict[str, UsageIdentityPromotionRecord],
) -> UsageSnapshotDocument:
    """Build one current strict usage document."""
    return UsageSnapshotDocument(
        schema_version=USAGE_SCHEMA_VERSION,
        accounts=accounts,
        identity_promotions=promotions,
    )


def promotion_identities(
    promotion: UsageIdentityPromotionRecord,
) -> tuple[ProviderIdentity | None, ProviderIdentity]:
    """Return one promotion's exact source and target identities."""
    source = (
        None
        if promotion.source_identity is None
        else ProviderIdentity(promotion.source_identity)
    )
    return source, ProviderIdentity(promotion.target_identity)


def without_usage_promotion(
    document: UsageSnapshotDocument,
    key: str,
) -> dict[str, UsageIdentityPromotionRecord]:
    """Return the current promotions except one completed account."""
    return {
        account_id: promotion
        for account_id, promotion in document.identity_promotions.items()
        if account_id != key
    }


def usage_snapshot_for_account(
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
    source_identity, target_identity = promotion_identities(promotion)
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


def merge_usage_snapshot(
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
        source_identity, target_identity = promotion_identities(pending)
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
    effective = _merge_usage(current, incoming)
    accounts[key] = usage_record(effective)
    return effective


def begin_usage_promotions(
    document: UsageSnapshotDocument,
    key: str,
    provider_id: ProviderId,
    current_identity: ProviderIdentity | None,
    target_identity: ProviderIdentity,
) -> dict[str, UsageIdentityPromotionRecord]:
    """Return the document promotions with one exact intent staged."""
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


def _merge_usage(
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


def _promotion(
    provider_id: ProviderId,
    source_identity: ProviderIdentity | None,
    target_identity: ProviderIdentity,
) -> UsageIdentityPromotionRecord:
    return UsageIdentityPromotionRecord(
        provider_id=provider_id.value,
        source_identity=(
            None if source_identity is None else str(source_identity)
        ),
        target_identity=str(target_identity),
    )


def _matches_promotion(
    promotion: UsageIdentityPromotionRecord,
    provider_id: ProviderId,
    provider_identity: ProviderIdentity,
    current_identity: ProviderIdentity | None,
) -> bool:
    source_identity, target_identity = promotion_identities(promotion)
    return (
        ProviderId(promotion.provider_id) is provider_id
        and target_identity == provider_identity
        and current_identity in {source_identity, target_identity}
    )
