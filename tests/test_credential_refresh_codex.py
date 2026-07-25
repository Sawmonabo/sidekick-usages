"""Managed Codex authority refresh and all-account maintenance tests."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    AccountUsageSnapshot,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    OperationKind,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import (
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.daemon.lifecycle.readiness import SupervisorReadiness
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.account import (
    CodexManagedAccountService,
)
from sidekick_usages.daemon.worker.codex import (
    CodexManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.paths import managed_codex_home
from sidekick_usages.persistence.snapshots.activity import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.activity import ACTIVITY_URL
from sidekick_usages.providers.codex.usage import USAGE_URL
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.managed import (
    managed_coordinator,
    managed_generation,
    managed_saved_account,
    managed_subscription,
    seed_managed_accounts,
)
from tests.test_support import REFERENCE_TIME, FixedClock

_MANAGED_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_MANAGED_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_MANAGED_AUTHORITY_A = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGED_AUTHORITY_B = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OLD_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NEW_GENERATION = "2026-07-24T10:01:00.000000000Z"
_CURRENT_USAGE = 25.0


class _ManagedMetricsHttp(HttpClient):
    """Return exact synthetic usage and activity for managed account B."""

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        if headers["ChatGPT-Account-Id"] != "acct-managed-b":
            raise AssertionError("Metrics crossed the managed account.")
        if url == USAGE_URL:
            return {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": _CURRENT_USAGE,
                        "reset_at": 1_800_000_000,
                    }
                },
            }
        if url == ACTIVITY_URL:
            return {
                "stats": {
                    "lifetime_tokens": 9_617_297_075,
                    "daily_usage_buckets": [
                        {
                            "start_date": "2026-04-07",
                            "tokens": 9_617_297_075,
                        }
                    ],
                }
            }
        raise AssertionError("Unexpected managed metrics route.")


def test_managed_codex_maintenance_continues_across_account_failure(
    tmp_path: Path,
) -> None:
    """One failed home cannot block another account's complete maintenance."""
    account_a = replace(
        managed_saved_account(
            _MANAGED_ACCOUNT_A,
            _MANAGED_AUTHORITY_A,
            "codex-a",
            "acct-managed-a",
            _OLD_GENERATION,
        ),
        credential_health=CredentialHealth.REFRESH_DUE,
    )
    account_b = replace(
        managed_saved_account(
            _MANAGED_ACCOUNT_B,
            _MANAGED_AUTHORITY_B,
            "codex-b",
            "acct-managed-b",
            _OLD_GENERATION,
        ),
        credential_health=CredentialHealth.REFRESH_DUE,
        heartbeat_enabled=True,
    )
    paths, store, private = seed_managed_accounts(
        tmp_path,
        (account_a, account_b),
        {
            _MANAGED_ACCOUNT_A: managed_auth(
                "acct-managed-wrong",
                _NEW_GENERATION,
            ),
            _MANAGED_ACCOUNT_B: managed_auth(
                "acct-managed-b",
                _NEW_GENERATION,
            ),
        },
    )
    coordinator = managed_coordinator(tmp_path, paths, store, private)
    clock = FixedClock()
    SupervisorReadiness(paths, clock).enroll_accounts()
    maintenance = tuple(
        operation
        for operation in OperationQueueStore(paths.durable_operations).due(
            clock.now()
        )
        if operation.kind is OperationKind.MAINTAIN
    )
    assert tuple(
        operation.required_account_id for operation in maintenance
    ) == (_MANAGED_ACCOUNT_A, _MANAGED_ACCOUNT_B)
    maintenance_a, maintenance_b = maintenance
    usage_snapshots = UsageSnapshotStore(paths.usage_snapshots)
    usage_snapshots.save(
        AccountUsageSnapshot(
            account_id=_MANAGED_ACCOUNT_A,
            provider_id=ProviderId.CODEX,
            provider_identity=ProviderIdentity("acct-managed-a"),
            plan="pro",
            report=UsageReport(
                windows=(
                    UsageWindow(
                        "5h",
                        51,
                        REFERENCE_TIME + timedelta(hours=2),
                    ),
                ),
                plan="pro",
            ),
            fetched_at=REFERENCE_TIME - timedelta(hours=1),
        )
    )
    selected = SelectedStateStore(paths.selected_state)
    selected_before = selected.save(
        SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=_MANAGED_ACCOUNT_A,
            provider_identity=ProviderIdentity("acct-managed-a"),
            runtime_generation=AuthorityGeneration(_OLD_GENERATION),
            verified_at=REFERENCE_TIME,
            outcome=ActivationOutcome.VERIFIED,
        )
    )
    executor = CodexManagedMaintenanceWorkerExecutor(
        coordinator,
        CodexManagedAccountService(
            coordinator,
            store,
            _ManagedMetricsHttp(),
            ActivitySnapshotStore(paths.activity_snapshots),
            usage_snapshots,
            clock,
        ),
        clock,
    )

    with OperationAuthorityLock(
        paths.durable_operations,
        _MANAGED_ACCOUNT_A,
    ).hold() as authority:
        maintained_a = executor.execute(
            maintenance_a,
            authority,
        )
    with OperationAuthorityLock(
        paths.durable_operations,
        _MANAGED_ACCOUNT_B,
    ).hold() as authority:
        maintained_b = executor.execute(
            maintenance_b,
            authority,
        )

    assert maintained_a.outcome is WorkerOutcome.ACTION_REQUIRED
    assert maintained_b.outcome is WorkerOutcome.SUCCEEDED
    assert selected.load(ProviderId.CODEX) == selected_before
    saved = {account.account_id: account for account in store.saved_accounts()}
    failed = saved[_MANAGED_ACCOUNT_A]
    assert managed_subscription(failed) == managed_subscription(account_a)
    assert failed.credential_health is CredentialHealth.RECONCILIATION_REQUIRED
    assert failed.last_refresh_status is RefreshStatus.FAILED

    advanced = saved[_MANAGED_ACCOUNT_B]
    assert managed_subscription(advanced).generation == AuthorityGeneration(
        _NEW_GENERATION
    )
    assert advanced.last_refresh_status is RefreshStatus.OK
    assert advanced.last_heartbeat_status is HeartbeatStatus.ACTIVE
    assert advanced.heartbeat_window_resets == (
        ("standard", datetime.fromtimestamp(1_800_000_000, UTC)),
    )
    assert managed_generation(private, _MANAGED_ACCOUNT_B) == _NEW_GENERATION
    assert managed_codex_home(paths, _MANAGED_ACCOUNT_A).name == str(
        _MANAGED_ACCOUNT_A
    )
    assert managed_codex_home(paths, _MANAGED_ACCOUNT_B).name == str(
        _MANAGED_ACCOUNT_B
    )
    stale = usage_snapshots.load(failed)
    current = usage_snapshots.load(advanced)
    assert stale is not None
    assert stale.fetched_at == REFERENCE_TIME - timedelta(hours=1)
    assert current is not None
    assert current.fetched_at == REFERENCE_TIME
    assert current.report.windows[0].utilization == _CURRENT_USAGE

    requests = [
        event
        for event in (
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        )
        if event.get("method") == "account/read"
        and event["params"]["refreshToken"]
    ]
    assert [Path(event["codex_home"]).name for event in requests] == [
        str(_MANAGED_ACCOUNT_A),
        str(_MANAGED_ACCOUNT_B),
    ]
    persisted = paths.accounts.read_bytes()
    assert b'"tokens"' not in persisted
    assert b"managed-refresh-" not in persisted
    assert b"managed-id-" not in persisted


@pytest.mark.parametrize(
    ("case", "expected_outcome", "expected_health"),
    [
        (
            "unchanged",
            CodexManagedOutcome.UNCHANGED,
            CredentialHealth.REFRESH_DUE,
        ),
        (
            "malformed",
            CodexManagedOutcome.MALFORMED,
            CredentialHealth.MALFORMED,
        ),
    ],
)
def test_managed_codex_refresh_fails_closed(
    tmp_path: Path,
    case: str,
    expected_outcome: CodexManagedOutcome,
    expected_health: CredentialHealth,
) -> None:
    """Distinct trust failures retain the prior no-secret authority."""
    account = managed_saved_account(
        _MANAGED_ACCOUNT_A,
        _MANAGED_AUTHORITY_A,
        "codex-a",
        "acct-managed-a",
        _OLD_GENERATION,
    )
    next_authority = {
        "unchanged": managed_auth(
            "acct-managed-a",
            _OLD_GENERATION,
        ),
        "malformed": b"{",
    }[case]
    paths, store, private = seed_managed_accounts(
        tmp_path,
        (account,),
        {_MANAGED_ACCOUNT_A: next_authority},
    )
    coordinator = managed_coordinator(tmp_path, paths, store, private)
    before = store.saved_accounts()[0]

    result = coordinator.refresh(_MANAGED_ACCOUNT_A)

    after = store.saved_accounts()[0]
    assert result.outcome is expected_outcome
    assert result.account == after
    assert after.authority == before.authority
    assert after.credential_health is expected_health
    assert after.last_refresh_status is RefreshStatus.FAILED
    assert (
        after.last_refresh_error_code
        == f"codex_managed_{expected_outcome.value}"
    )
    persisted = paths.accounts.read_bytes()
    assert b'"tokens"' not in persisted
    assert b"managed-refresh-" not in persisted
    assert b"managed-id-" not in persisted
    assert "managed-refresh-" not in repr(result)
