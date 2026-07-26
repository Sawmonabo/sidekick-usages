"""Shared activity snapshot validation and conversion policy."""

from dataclasses import replace

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    ProviderTokenActivitySnapshot,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import ActivitySnapshotError
from sidekick_usages.persistence.schema.activity import (
    ActivitySnapshotDecodeError,
    ActivitySnapshotDocument,
    ActivitySnapshotRecord,
    account_activity_snapshot,
    activity_record,
    decode_activity_snapshot_document,
    provider_activity_record,
    provider_activity_snapshot,
)
from sidekick_usages.persistence.types.artifact import (
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)


def decode_activity_document(payload: bytes) -> ActivitySnapshotDocument:
    """Decode one strict activity document with the typed error vocabulary."""
    try:
        return decode_activity_snapshot_document(payload)
    except ActivitySnapshotDecodeError:
        raise ActivitySnapshotError(
            ActivitySnapshotFailureKind.MALFORMED
        ) from None


def activity_identity_key(
    provider_id: ProviderId,
    account_id: str,
) -> Sha256Digest:
    """Derive the stable non-secret key for one provider account."""
    try:
        value = f"{provider_id.value}\0{account_id}".encode()
    except UnicodeEncodeError:
        raise ValueError(
            "Provider account identity must be valid UTF-8."
        ) from None
    return sha256_digest(value)


def activity_snapshot_for_account(
    document: ActivitySnapshotDocument,
    account: SavedAccount,
    provider_account_id: str,
) -> AccountTokenActivitySnapshot | None:
    """Read one account snapshot from an already-decoded document."""
    record = document.accounts.get(
        str(activity_identity_key(account.provider_id, provider_account_id))
    )
    return (
        None
        if record is None
        else account_activity_snapshot(provider_account_id, record)
    )


def merge_activity_snapshot(
    accounts: dict[str, ActivitySnapshotRecord],
    snapshot: AccountTokenActivitySnapshot,
) -> AccountTokenActivitySnapshot:
    """Merge one observation into an already-decoded activity document."""
    key = str(
        activity_identity_key(
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
    effective = _merge_activity(current, snapshot)
    accounts[key] = activity_record(effective)
    return effective


def activity_snapshot_for_provider(
    document: ActivitySnapshotDocument,
    provider_id: ProviderId,
) -> ProviderTokenActivitySnapshot | None:
    """Read one provider snapshot from an already-decoded document."""
    record = document.providers.get(provider_id.value)
    return None if record is None else provider_activity_snapshot(record)


def merge_provider_activity_snapshot(
    providers: dict[str, ActivitySnapshotRecord],
    snapshot: ProviderTokenActivitySnapshot,
) -> ProviderTokenActivitySnapshot:
    """Merge one provider observation into an activity document."""
    key = snapshot.provider_id.value
    current_record = providers.get(key)
    current = (
        None
        if current_record is None
        else provider_activity_snapshot(current_record)
    )
    effective = _merge_activity(current, snapshot)
    providers[key] = provider_activity_record(effective)
    return effective


def _merge_activity[
    Snapshot: AccountTokenActivitySnapshot | ProviderTokenActivitySnapshot
](
    current: Snapshot | None,
    incoming: Snapshot,
) -> Snapshot:
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
