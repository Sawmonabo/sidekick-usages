"""Atomic Codex account-authority and private-bundle refresh tests."""

import base64
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Never

import pytest

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.codex import (
    CodexCredentialCoordinator,
    private_codex_home,
)
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshArtifacts,
    CredentialRefreshCrashPoint,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshStateKind,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import ReplaceFailedError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.models.artifact import ExpectedAuthority
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
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
    """Build one deterministic JWT-shaped access credential."""

    def encode(value: Mapping[str, object]) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{
            encode(
                {
                    'https://api.openai.com/auth': {
                        'chatgpt_account_id': _ACCOUNT_ID,
                        'generation': generation,
                    }
                }
            )
        }.sig"
    )


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
