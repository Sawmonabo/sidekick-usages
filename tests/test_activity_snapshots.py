"""Behavioral tests for durable authoritative activity snapshots."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    AccountTokenActivitySnapshot,
    CodexCredentials,
    TokenActivitySummary,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.persistence.activity_snapshots import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.errors import ActivitySnapshotError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)

_FETCHED_AT = datetime(2026, 7, 11, 4, 30, tzinfo=UTC)
_ACCOUNT_COUNT = 2
_SHA256_HEX_LENGTH = 64


def _account(label: str, account_id: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=f"test-only-{label}-access",
            expiry=UnknownExpiry(),
            account_id=account_id,
        ),
    )


def _snapshot(
    account: Account,
    total: int,
    since: date | None,
    fetched_at: datetime = _FETCHED_AT,
) -> AccountTokenActivitySnapshot:
    assert account.provider_account_id is not None
    return AccountTokenActivitySnapshot(
        provider_id=ProviderId.CODEX,
        provider_account_id=account.provider_account_id,
        summary=TokenActivitySummary(
            total_tokens=total,
            scope=TokenActivityScope.ACCOUNT,
            since=since,
        ),
        fetched_at=fetched_at,
    )


def _store(tmp_path: Path) -> ActivitySnapshotStore:
    path = tmp_path / "state" / "token-activity.json"
    PersistenceFilesystem(path).repair_parent_permissions()
    return ActivitySnapshotStore(path)


def test_snapshots_round_trip_by_hashed_identity_without_account_metadata(
    tmp_path: Path,
) -> None:
    """Distinct account snapshots persist without labels or raw identities."""
    store = _store(tmp_path)
    first = _account("first-label", "acct_private_first")
    second = _account("second-label", "acct_private_second")
    first_snapshot = _snapshot(first, 7_449_473_297, date(2026, 4, 7))
    second_snapshot = _snapshot(second, 900_000_000, date(2026, 3, 30))

    assert store.save(first_snapshot) == first_snapshot
    assert store.save(second_snapshot) == second_snapshot
    assert store.load(first) == first_snapshot
    assert store.load(second) == second_snapshot

    persisted = store.path.read_text(encoding="utf-8")
    document = json.loads(persisted)
    assert len(document["accounts"]) == _ACCOUNT_COUNT
    assert all(len(key) == _SHA256_HEX_LENGTH for key in document["accounts"])
    for private_value in (
        "first-label",
        "second-label",
        "acct_private_first",
        "acct_private_second",
        "test-only",
    ):
        assert private_value not in persisted


def test_snapshot_updates_cannot_regress_newer_truth_or_carry_false_dates(
    tmp_path: Path,
) -> None:
    """Timestamp order and verified-date preservation govern replacement."""
    store = _store(tmp_path)
    account = _account("account", "acct_private")
    initial = _snapshot(account, 100, date(2026, 4, 7))
    assert store.save(initial) == initial

    newer_without_buckets = _snapshot(
        account,
        125,
        None,
        _FETCHED_AT + timedelta(minutes=1),
    )
    preserved = store.save(newer_without_buckets)
    assert preserved.summary == replace(
        newer_without_buckets.summary,
        since=date(2026, 4, 7),
    )

    older = _snapshot(
        account,
        110,
        date(2026, 4, 8),
        _FETCHED_AT - timedelta(minutes=1),
    )
    assert store.save(older) == preserved

    regressed = _snapshot(
        account,
        80,
        None,
        _FETCHED_AT + timedelta(minutes=2),
    )
    replaced = store.save(regressed)
    assert replaced == regressed
    assert replaced.summary.since is None

    conflict = replace(
        regressed,
        summary=replace(regressed.summary, total_tokens=81),
    )
    with pytest.raises(ActivitySnapshotError) as captured:
        store.save(conflict)
    assert captured.value.kind is ActivitySnapshotFailureKind.CONFLICT
    assert store.load(account) == regressed


def test_malformed_snapshot_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    """Invalid durable state is reported and preserved for recovery."""
    store = _store(tmp_path)
    account = _account("account", "acct_private")
    malformed = b'{"schema_version":1,"schema_version":1,"accounts":{}}\n'
    PersistenceFilesystem(store.path).commit_opaque_private(malformed)

    with pytest.raises(ActivitySnapshotError) as loaded:
        store.load(account)
    assert loaded.value.kind is ActivitySnapshotFailureKind.MALFORMED
    with pytest.raises(ActivitySnapshotError) as saved:
        store.save(_snapshot(account, 1, date(2026, 4, 7)))
    assert saved.value.kind is ActivitySnapshotFailureKind.MALFORMED
    assert store.path.read_bytes() == malformed
