"""Behavioral tests for durable authoritative activity snapshots."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    AccountTokenActivitySnapshot,
    CodexCredentials,
    ProviderTokenActivitySnapshot,
    TokenActivitySummary,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.persistence.errors import ActivitySnapshotError
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.snapshots.activity.store import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)
from tests.support.accounts import saved_account

_FETCHED_AT = datetime(2026, 7, 11, 4, 30, tzinfo=UTC)
_ACCOUNT_COUNT = 2
_SHA256_HEX_LENGTH = 64
_MALFORMED_DERIVED_CACHE = (
    b'{"schema_version":1,"schema_version":1,"accounts":{}}\n'
)


def _account(label: str, account_id: str) -> SavedAccount:
    return saved_account(
        Account(
            label=AccountLabel(label),
            credentials=CodexCredentials(
                access_token=f"test-only-{label}-access",
                expiry=UnknownExpiry(),
                account_id=account_id,
            ),
        )
    )


def _snapshot(
    account: SavedAccount,
    total: int,
    since: date | None,
    fetched_at: datetime = _FETCHED_AT,
) -> AccountTokenActivitySnapshot:
    assert account.provider_identity is not None
    return AccountTokenActivitySnapshot(
        provider_id=ProviderId.CODEX,
        provider_account_id=str(account.provider_identity),
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
    """Account and provider scopes round-trip without private identity."""
    store = _store(tmp_path)
    first = _account("first-label", "acct_private_first")
    second = _account("second-label", "acct_private_second")
    first_snapshot = _snapshot(first, 7_449_473_297, date(2026, 4, 7))
    second_snapshot = _snapshot(second, 900_000_000, date(2026, 3, 30))
    provider_snapshot = ProviderTokenActivitySnapshot(
        provider_id=ProviderId.CLAUDE,
        summary=TokenActivitySummary(
            total_tokens=903_464_085,
            scope=TokenActivityScope.LOCAL_INSTALLATION,
            since=date(2025, 12, 28),
        ),
        fetched_at=_FETCHED_AT,
    )
    assert store.save_many(
        (first_snapshot, second_snapshot),
        (provider_snapshot,),
    ) == (
        (first_snapshot, second_snapshot),
        (provider_snapshot,),
    )
    assert store.load(first) == first_snapshot
    assert store.load(second) == second_snapshot
    account_snapshots, provider_snapshots = store.load_all((first, second))
    assert dict(account_snapshots) == {
        first.account_id: first_snapshot,
        second.account_id: second_snapshot,
    }
    assert provider_snapshots == (provider_snapshot,)

    persisted = store.path.read_text(encoding="utf-8")
    document = json.loads(persisted)
    assert len(document["accounts"]) == _ACCOUNT_COUNT
    assert tuple(document["providers"]) == (ProviderId.CLAUDE.value,)
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


def test_malformed_snapshot_repairs_only_with_fresh_activity(
    tmp_path: Path,
) -> None:
    """Passive reads fail closed until fresh activity repairs the cache."""
    store = _store(tmp_path)
    account = _account("account", "acct_private")
    PersistenceFilesystem(store.path).commit_opaque_private(
        _MALFORMED_DERIVED_CACHE
    )

    with pytest.raises(ActivitySnapshotError) as loaded:
        store.load(account)
    assert loaded.value.kind is ActivitySnapshotFailureKind.MALFORMED
    assert store.path.read_bytes() == _MALFORMED_DERIVED_CACHE
    assert store.save_many((), ()) == ((), ())
    assert store.path.read_bytes() == _MALFORMED_DERIVED_CACHE

    fresh = _snapshot(account, 1, date(2026, 4, 7))
    assert store.save(fresh) == fresh
    assert store.load(account) == fresh
