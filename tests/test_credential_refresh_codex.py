"""Atomic Codex account-authority and private-bundle refresh tests."""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    AccountUsageSnapshot,
    CodexCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.codex.coordinator import (
    CodexCredentialCoordinator,
    private_codex_home,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.account import (
    CodexManagedAccountService,
)
from sidekick_usages.daemon.worker.codex import (
    CodexManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.paths import managed_codex_home
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshArtifacts,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshCrashPoint,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import ReplaceFailedError
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import ExpectedAuthority
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.snapshots.activity import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.codex.activity import ACTIVITY_URL
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
    validate_auth_bundle_matches_account,
)
from sidekick_usages.providers.codex.usage import USAGE_URL
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.codex.auth import codex_jwt, managed_auth
from tests.fakes.codex.managed import (
    managed_coordinator,
    managed_generation,
    managed_saved_account,
    managed_subscription,
    seed_managed_accounts,
)
from tests.test_credential_refresh_support import (
    CrashAt,
    SimulatedCrashError,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

_LABEL = AccountLabel("codex-team")
_ACCOUNT_ID = "acct-refresh"
_CONFIG = f"{CODEX_FILE_AUTH_CONFIG}\n".encode()
_MANAGED_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_MANAGED_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_MANAGED_AUTHORITY_A = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGED_AUTHORITY_B = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OLD_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NEW_GENERATION = "2026-07-24T10:01:00.000000000Z"
_MAINTENANCE_A = OperationId("33333333-3333-4333-8333-333333333333")
_MAINTENANCE_B = OperationId("44444444-4444-4444-8444-444444444444")
_CURRENT_USAGE = 25.0


class _ManagedMetricsHttp(HttpClient):
    """Return exact synthetic usage and activity for managed account B."""

    def __init__(self) -> None:
        self.account_ids: list[str] = []

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        account_id = headers["ChatGPT-Account-Id"]
        if account_id != "acct-managed-b":
            raise AssertionError("Metrics crossed the managed account.")
        self.account_ids.append(account_id)
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


def _maintenance_operation(
    operation_id: OperationId,
    account_id: SidekickAccountId,
) -> DueOperation:
    return DueOperation(
        operation_id=operation_id,
        provider_id=ProviderId.CODEX,
        account_id=account_id,
        kind=OperationKind.MAINTAIN,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )


class _AuthorityFailure:
    """Fail after private targets are applied but before authority."""

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> Never:
        del payload, expected_source
        raise ReplaceFailedError


class _SimulatedAuthorityCrash(BaseException):
    """Model process loss before account authority publication."""


class _AuthorityCrash:
    """Crash after private targets are applied but before authority."""

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> Never:
        del payload, expected_source
        raise _SimulatedAuthorityCrash


class _InjectedLock:
    """Yield one deterministic account-authority committer."""

    def __init__(
        self,
        transaction: _AuthorityFailure | _AuthorityCrash,
    ) -> None:
        self._transaction = transaction

    @contextmanager
    def hold(self) -> Iterator[_AuthorityFailure | _AuthorityCrash]:
        yield self._transaction


def _jwt(generation: str) -> str:
    return codex_jwt(_ACCOUNT_ID, generation)


def _account(home: Path, generation: str = "old") -> Account:
    return Account(
        label=_LABEL,
        credentials=CodexCredentials(
            access_token=_jwt(generation),
            refresh_token=f"refresh-{generation}",
            id_token=f"id-{generation}",
            account_id=_ACCOUNT_ID,
            expiry=KnownExpiry(
                REFERENCE_TIME.replace(microsecond=0) + timedelta(hours=1)
            ),
            auth_home=str(home),
            auth_last_refresh=(
                "2026-07-12T00:00:01Z"
                if generation == "new"
                else "2026-07-12T00:00:00Z"
            ),
        ),
        plan="pro",
    )


def _auth(generation: str) -> bytes:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "future_metadata": {"preserved": True},
            "last_refresh": (
                "2026-07-12T00:00:01Z"
                if generation == "new"
                else "2026-07-12T00:00:00Z"
            ),
            "tokens": {
                "access_token": _jwt(generation),
                "refresh_token": f"refresh-{generation}",
                "id_token": f"id-{generation}",
                "account_id": _ACCOUNT_ID,
            },
        }
    ).encode()


class _CodexRefreshProvider(Provider):
    id = ProviderId.CODEX
    display_name = "Synthetic Codex"

    def __init__(self) -> None:
        self.calls: list[Account] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        raise AssertionError("credential detection was unexpected")

    def credentials_from_token(self, token: str) -> CredentialDetection:
        del token
        raise AssertionError("token parsing was unexpected")

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        raise AssertionError("usage was unexpected")

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        self.calls.append(account)
        previous = account.credentials
        assert isinstance(previous, CodexCredentials)
        return RefreshSuccess(
            credentials=replace(
                previous,
                access_token=_jwt("new"),
                refresh_token="refresh-new",
                id_token="id-new",
                auth_last_refresh="2026-07-12T00:00:01Z",
            )
        )


def _seed(
    root: Path,
) -> tuple[AccountStore, PrivateCredentialTree, Path]:
    """Seed one account and exact matching canonical private bundle."""
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts).repair_parent_permissions()
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    store = AccountStore(paths.accounts, private).load()
    bundle = private_codex_home(private.root, str(_LABEL))
    account = _account(bundle)
    store.persist_credentials(
        account,
        private_bundle=PreparedPrivateBundleWrite(
            path=bundle,
            files={CODEX_AUTH_FILE: _auth("old"), CODEX_CONFIG_FILE: _CONFIG},
            expected_bundle_present=False,
            expected_files={CODEX_AUTH_FILE: None, CODEX_CONFIG_FILE: None},
        ),
    )
    return store, private, bundle


def _reopen(
    root: Path,
    private: PrivateCredentialTree,
    transaction: _AuthorityFailure | _AuthorityCrash | None = None,
) -> AccountStore:
    """Open the same authority with an optional failing commit boundary."""
    paths = make_application_paths(root)
    if transaction is None:
        return AccountStore(paths.accounts, private).load()
    return AccountStore(
        paths.accounts,
        private,
        lock_factory=lambda _filesystem: _InjectedLock(transaction),
    ).load()


def _coordinator(
    root: Path,
    store: AccountStore,
    private: PrivateCredentialTree,
    provider: _CodexRefreshProvider,
) -> CredentialRefreshCoordinator:
    """Compose one synthetic rich Codex refresh application boundary."""
    clock = FixedClock()
    return CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        CredentialRefreshTransactions(
            store,
            make_application_paths(root).credential_refresh,
        ),
        clock=clock,
        codex=CodexCredentialCoordinator(store, private, clock=clock),
        resolver=credential_resolver_for(store, private),
    )


def _assert_generation(
    store: AccountStore,
    private: PrivateCredentialTree,
    bundle: Path,
    generation: str,
) -> None:
    """Prove authority and private auth hold the same generation."""
    saved = store.get(str(_LABEL))
    auth = private.read_bundle_file(bundle, CODEX_AUTH_FILE)
    assert saved is not None
    assert auth is not None
    assert saved.access_token == _jwt(generation)
    assert validate_auth_bundle_matches_account(auth, saved) is None
    tokens = json.loads(auth)["tokens"]
    assert tokens["access_token"] == saved.access_token
    assert tokens["refresh_token"] == saved.refresh_token
    assert tokens["id_token"] == saved.codex_id_token


def test_codex_rotation_commits_matching_account_and_private_bundle(
    tmp_path: Path,
) -> None:
    """A successful rotation publishes one mutually consistent state."""
    store, private, bundle = _seed(tmp_path)
    provider = _CodexRefreshProvider()
    coordinator = _coordinator(tmp_path, store, private, provider)

    coordinator.refresh(
        provider_id=ProviderId.CODEX,
        label=_LABEL,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    auth = private.read_bundle_file(bundle, CODEX_AUTH_FILE)
    assert auth is not None
    _assert_generation(store, private, bundle, "new")
    assert json.loads(auth)["future_metadata"] == {"preserved": True}
    assert len(provider.calls) == 1


def test_codex_stage_is_one_recoverable_account_and_bundle_envelope(
    tmp_path: Path,
) -> None:
    """No process-loss boundary separates staged account and bundle."""
    store, private, bundle = _seed(tmp_path)
    provider = _CodexRefreshProvider()
    root = make_application_paths(tmp_path).credential_refresh
    clock = FixedClock()
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        CredentialRefreshTransactions(
            store,
            root,
            faults=CrashAt(CredentialRefreshCrashPoint.STAGE_WRITTEN),
        ),
        clock=clock,
        codex=CodexCredentialCoordinator(store, private, clock=clock),
        resolver=credential_resolver_for(store, private),
    )

    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            provider_id=ProviderId.CODEX,
            label=_LABEL,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    evidence = next(path for path in root.iterdir() if path.is_dir())
    assert {path.name for path in evidence.iterdir()} == {
        "intent.json",
        "replacement.json",
    }
    calls_before_recovery = len(provider.calls)
    CredentialRefreshTransactions(store, root).recover()
    _assert_generation(store, private, bundle, "new")
    assert len(provider.calls) == calls_before_recovery == 1


def test_codex_bundle_failure_rolls_back_then_recovers_both_targets(
    tmp_path: Path,
) -> None:
    """A caught authority failure leaves old state before local recovery."""
    _store, private, bundle = _seed(tmp_path)
    failed_store = _reopen(tmp_path, private, _AuthorityFailure())
    provider = _CodexRefreshProvider()

    with pytest.raises(ReplaceFailedError):
        _coordinator(tmp_path, failed_store, private, provider).refresh(
            provider_id=ProviderId.CODEX,
            label=_LABEL,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    _assert_generation(failed_store, private, bundle, "old")
    calls_before_recovery = len(provider.calls)
    recovered = _reopen(tmp_path, private)
    CredentialRefreshTransactions(
        recovered,
        make_application_paths(tmp_path).credential_refresh,
    ).recover()
    _assert_generation(recovered, private, bundle, "new")
    assert len(provider.calls) == calls_before_recovery


def test_codex_bundle_crash_recovers_without_newer_account_authority(
    tmp_path: Path,
) -> None:
    """Process loss converges the nested and outer transactions together."""
    _store, private, bundle = _seed(tmp_path)
    crashing_store = _reopen(tmp_path, private, _AuthorityCrash())
    provider = _CodexRefreshProvider()

    with pytest.raises(_SimulatedAuthorityCrash):
        _coordinator(tmp_path, crashing_store, private, provider).refresh(
            provider_id=ProviderId.CODEX,
            label=_LABEL,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    crashed = crashing_store.get(str(_LABEL))
    assert crashed is not None
    assert crashed.access_token == _jwt("old")
    calls_before_recovery = len(provider.calls)
    recovered = _reopen(tmp_path, private)
    _assert_generation(recovered, private, bundle, "old")
    CredentialRefreshTransactions(
        recovered,
        make_application_paths(tmp_path).credential_refresh,
    ).recover()
    _assert_generation(recovered, private, bundle, "new")
    assert len(provider.calls) == calls_before_recovery


def test_malformed_codex_combined_stage_is_blocked_without_publication(
    tmp_path: Path,
) -> None:
    """Doctor and recovery fail closed on untrusted bundle-stage bytes."""
    store, private, bundle = _seed(tmp_path)
    provider = _CodexRefreshProvider()
    clock = FixedClock()
    root = make_application_paths(tmp_path).credential_refresh
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        CredentialRefreshTransactions(
            store,
            root,
            faults=CrashAt(CredentialRefreshCrashPoint.STAGE_COMPLETE),
        ),
        clock=clock,
        codex=CodexCredentialCoordinator(store, private, clock=clock),
        resolver=credential_resolver_for(store, private),
    )
    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            provider_id=ProviderId.CODEX,
            label=_LABEL,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )
    combined_stage = next(root.glob("*/replacement.json"))
    combined_stage.write_bytes(b"{")

    assert (
        CredentialRefreshArtifacts(root).assess().kind
        is CredentialRefreshStateKind.BLOCKED
    )
    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        CredentialRefreshTransactions(store, root).recover()
    _assert_generation(store, private, bundle, "old")


def test_managed_codex_maintenance_continues_across_account_failure(
    tmp_path: Path,
) -> None:
    """A failed selected home cannot block another managed Codex account."""
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
    clock = FixedClock()
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
            _maintenance_operation(_MAINTENANCE_A, _MANAGED_ACCOUNT_A),
            authority,
        )
    with OperationAuthorityLock(
        paths.durable_operations,
        _MANAGED_ACCOUNT_B,
    ).hold() as authority:
        maintained_b = executor.execute(
            _maintenance_operation(_MAINTENANCE_B, _MANAGED_ACCOUNT_B),
            authority,
        )

    assert maintained_a.outcome is WorkerOutcome.ACTION_REQUIRED
    assert maintained_b.outcome is WorkerOutcome.SUCCEEDED
    assert selected.load(ProviderId.CODEX) == selected_before
    saved = {account.account_id: account for account in store.saved_accounts()}
    failed = saved[_MANAGED_ACCOUNT_A]
    failed_authority = managed_subscription(failed)
    assert failed_authority == managed_subscription(account_a)
    assert failed.credential_health is CredentialHealth.RECONCILIATION_REQUIRED
    assert failed.last_refresh_at == REFERENCE_TIME
    assert failed.last_refresh_status is RefreshStatus.FAILED

    advanced = saved[_MANAGED_ACCOUNT_B]
    advanced_authority = managed_subscription(advanced)
    assert advanced_authority.provider_identity == ProviderIdentity(
        "acct-managed-b"
    )
    assert advanced_authority.generation == AuthorityGeneration(
        _NEW_GENERATION
    )
    assert advanced.last_refresh_at == REFERENCE_TIME
    assert advanced.last_refresh_status is RefreshStatus.OK
    assert advanced.last_heartbeat_at == REFERENCE_TIME
    assert advanced.last_heartbeat_status is HeartbeatStatus.ACTIVE
    assert advanced.heartbeat_window_resets == (
        (
            "standard",
            datetime.fromtimestamp(1_800_000_000, UTC),
        ),
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
    assert [
        (Path(event["codex_home"]).name, event["params"]["refreshToken"])
        for event in requests
    ] == [
        (str(_MANAGED_ACCOUNT_A), True),
        (str(_MANAGED_ACCOUNT_B), True),
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
    assert after.last_refresh_at == REFERENCE_TIME
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
