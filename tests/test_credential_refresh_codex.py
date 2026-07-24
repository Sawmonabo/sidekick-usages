"""Atomic Codex account-authority and private-bundle refresh tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Never

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    UsageReport,
)
from sidekick_usages.core.types import (
    AccountLabel,
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
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
    validate_auth_bundle_matches_account,
)
from tests.fakes.codex import (
    codex_jwt,
    managed_auth,
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
        """Yield the injected held-lock transaction."""
        yield self._transaction


def _jwt(generation: str) -> str:
    """Build one legacy refresh fixture for its fixed account."""
    return codex_jwt(_ACCOUNT_ID, generation)


def _account(home: Path, generation: str = "old") -> Account:
    """Build one refreshable saved Codex account."""
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
    """Encode a strict private Codex auth bundle fixture."""
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
    """Return one validated synthetic Codex rotation."""

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


def test_managed_codex_homes_read_and_refresh_independently(
    tmp_path: Path,
) -> None:
    """Two official homes advance independently without persisting tokens."""
    account_a = managed_saved_account(
        _MANAGED_ACCOUNT_A,
        _MANAGED_AUTHORITY_A,
        "codex-a",
        "acct-managed-a",
        _OLD_GENERATION,
    )
    account_b = managed_saved_account(
        _MANAGED_ACCOUNT_B,
        _MANAGED_AUTHORITY_B,
        "codex-b",
        "acct-managed-b",
        _OLD_GENERATION,
    )
    paths, store, private = seed_managed_accounts(
        tmp_path,
        (account_a, account_b),
        {
            _MANAGED_ACCOUNT_A: managed_auth(
                "acct-managed-a",
                _NEW_GENERATION,
            ),
            _MANAGED_ACCOUNT_B: managed_auth(
                "acct-managed-b",
                _NEW_GENERATION,
            ),
        },
    )
    coordinator = managed_coordinator(tmp_path, paths, store, private)

    read_a = coordinator.read(_MANAGED_ACCOUNT_A)
    read_b = coordinator.read(_MANAGED_ACCOUNT_B)
    refreshed_a = coordinator.refresh(_MANAGED_ACCOUNT_A)

    assert read_a.outcome is CodexManagedOutcome.HEALTHY
    assert read_b.outcome is CodexManagedOutcome.HEALTHY
    assert refreshed_a.outcome is CodexManagedOutcome.HEALTHY
    assert managed_generation(private, _MANAGED_ACCOUNT_B) == _OLD_GENERATION

    refreshed_b = coordinator.refresh(_MANAGED_ACCOUNT_B)

    assert refreshed_b.outcome is CodexManagedOutcome.HEALTHY
    saved = {account.account_id: account for account in store.saved_accounts()}
    for account_id, identity in (
        (_MANAGED_ACCOUNT_A, "acct-managed-a"),
        (_MANAGED_ACCOUNT_B, "acct-managed-b"),
    ):
        authority = managed_subscription(saved[account_id])
        assert authority.provider_identity == ProviderIdentity(identity)
        assert authority.generation == AuthorityGeneration(_NEW_GENERATION)
        assert managed_generation(private, account_id) == _NEW_GENERATION
        assert managed_codex_home(paths, account_id).name == str(account_id)

    requests = [
        event
        for event in (
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        )
        if event.get("method") == "account/read"
    ]
    assert [
        (Path(event["codex_home"]).name, event["params"]["refreshToken"])
        for event in requests
    ] == [
        (str(_MANAGED_ACCOUNT_A), False),
        (str(_MANAGED_ACCOUNT_B), False),
        (str(_MANAGED_ACCOUNT_A), True),
        (str(_MANAGED_ACCOUNT_B), True),
    ]
    persisted = paths.accounts.read_bytes()
    assert b'"tokens"' not in persisted
    assert b"managed-refresh-" not in persisted
    assert b"managed-id-" not in persisted
    assert "managed-refresh-" not in repr((read_a, read_b))
    assert "managed-refresh-" not in repr((refreshed_a, refreshed_b))


@pytest.mark.parametrize(
    ("case", "expected_outcome", "expected_health"),
    [
        (
            "wrong_identity",
            CodexManagedOutcome.REJECTED,
            CredentialHealth.RECONCILIATION_REQUIRED,
        ),
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
        "wrong_identity": managed_auth(
            "acct-managed-wrong",
            _NEW_GENERATION,
        ),
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
