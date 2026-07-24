"""Load-bearing behavior tests for typed usage orchestration."""

import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    CodexCredentials,
    Credentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials import (
    CredentialRefreshResult,
    CredentialRefreshSuccess,
    CredentialUpdateResult,
    CredentialUpdateSuccess,
)
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialResolver,
    EmbeddedAccountResolver,
)
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import (
    CredentialRefresher,
    TokenMaintenanceService,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    ReplaceFailedError,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
)
from sidekick_usages.usage import (
    AccountUsage,
    CredentialCoordinator,
    FetchFailureKind,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PersistenceFailure,
    ProviderPayloadFailure,
    RateLimitFailure,
    RefreshRejectedFailure,
    TransientFailure,
    UnknownProviderFailure,
    UsageCheckService,
)
from tests.test_support import REFERENCE_TIME, FixedClock, saved_account

type FetchStep = UsageReport | UsageError

_RETRY_AFTER_SECONDS = 17


def _copy_account(account: Account) -> Account:
    """Return an independent mutable account for the test boundary."""
    resets = account.heartbeat_window_resets
    return replace(
        account,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
    )


class InMemoryAccountStore(AccountStore):
    """Small AccountStore test double preserving its public contract."""

    def __init__(
        self,
        accounts: tuple[Account, ...],
        *,
        persist_error: PersistenceError | None = None,
    ) -> None:
        self._saved = {
            str(account.label): _copy_account(account) for account in accounts
        }
        self.persist_error = persist_error
        self.persisted: list[Account] = []
        self.iterations = 0
        self.filters: list[ProviderId] = []

    def __iter__(self) -> Iterator[Account]:
        self.iterations += 1
        return iter(tuple(_copy_account(a) for a in self._saved.values()))

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        self.filters.append(provider_id)
        return [
            _copy_account(account)
            for account in self._saved.values()
            if account.provider_id is provider_id
        ]

    def get(
        self,
        label: str,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        account = self._saved.get(label)
        if account is None or (
            provider_id is not None and account.provider_id is not provider_id
        ):
            return None
        return _copy_account(account)

    def persist(self, account: Account) -> None:
        if self.persist_error is not None:
            raise self.persist_error
        saved = _copy_account(account)
        self._saved[str(saved.label)] = saved
        self.persisted.append(_copy_account(saved))

    def saved_accounts(self) -> tuple[SavedAccount, ...]:
        """Return stable synthetic metadata for resolver-bound scenarios."""
        return tuple(
            saved_account(account) for account in self._saved.values()
        )

    def saved(self, label: str) -> Account:
        """Return one independent durable account for assertions."""
        return _copy_account(self._saved[label])


class ScriptedProvider(Provider):
    """Provider double exposing fetch, refresh, and mutation events."""

    display_name = "Test provider"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        provider_id: ProviderId,
        fetch_steps: dict[str, list[FetchStep]],
        *,
        account_id_on_fetch: str | None = None,
    ) -> None:
        self.id = provider_id
        self.fetch_steps = fetch_steps
        self.account_id_on_fetch = account_id_on_fetch
        self.events: list[str] = []
        self.fetch_tokens: list[str] = []
        self.fetch_scopes: list[tuple[str, ...] | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        del token
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.UNSUPPORTED,
            message="Manual test credentials are unsupported.",
        )

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del http
        label = str(account.label)
        self.events.append(f"fetch:{label}")
        self.fetch_tokens.append(account.access_token)
        self.fetch_scopes.append(account.scopes)
        if self.account_id_on_fetch is not None:
            credentials = account.credentials
            if not isinstance(credentials, CodexCredentials):
                raise AssertionError("Account-id mutation requires Codex.")
            account.credentials = replace(
                credentials,
                account_id=self.account_id_on_fetch,
            )
        step = self.fetch_steps[label].pop(0)
        if isinstance(step, UsageError):
            raise step
        return step

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del account, http
        raise AssertionError(
            "Usage orchestration must refresh through CredentialRefresher."
        )


class RecordingCredentialResolver:
    """Record exact lease scope around provider calls."""

    def __init__(self, store: InMemoryAccountStore) -> None:
        self._store = store
        self._embedded = EmbeddedAccountResolver()
        self.events: list[str] = []

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        return self._open(account)

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
    ) -> Iterator[AuthenticatedSavedAccount]:
        self.events.append(f"open:{account.label}")
        runtime = self._store.get(
            str(account.label),
            provider_id=account.provider_id,
        )
        if runtime is None:
            raise AssertionError("Resolver target disappeared.")
        with self._embedded.open(runtime) as authenticated:
            yield authenticated
        self.events.append(f"close:{account.label}")


class ScriptedCredentialCoordinator(CredentialRefresher):
    """Script the already-coordinated credential-service boundary."""

    def __init__(
        self,
        store: InMemoryAccountStore,
        steps: tuple[CredentialRefreshResult | UsageError, ...] = (),
    ) -> None:
        self.store = store
        self.steps = list(steps)
        self.calls: list[str] = []

    def refresh(
        self,
        *,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        del reason
        account = self.store.get(str(label))
        if account is None:
            raise AssertionError("Scripted refresh target disappeared.")
        self.calls.append(str(label))
        step = (
            self.steps.pop(0)
            if self.steps
            else CredentialRefreshSuccess(account.label)
        )
        if isinstance(step, UsageError):
            raise step
        if isinstance(step, ProviderFailure):
            return step
        saved = _copy_account(account)
        credentials = saved.credentials
        access_token = f"test-only-{account.label}-refreshed"
        if isinstance(credentials, ClaudeLoginCredentials):
            saved.credentials = replace(
                credentials,
                access_token=access_token,
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            )
        elif isinstance(credentials, CodexCredentials):
            saved.credentials = replace(
                credentials,
                access_token=access_token,
                expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            )
        else:
            saved.credentials = replace(
                credentials,
                access_token=access_token,
            )
        self.store.persist(saved)
        return step

    def persist_provider_update(
        self,
        account: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
    ) -> CredentialUpdateResult:
        current = self.store.get(str(account.label))
        assert current is not None
        assert current.credentials == expected_credentials
        assert current.plan == expected_plan
        current.credentials = account.credentials
        current.plan = account.plan
        self.store.persist(current)
        return CredentialUpdateSuccess(account.label)


@pytest.fixture
def http() -> Iterator[HttpClient]:
    """Yield a real idle HTTP facade; providers remain injected fakes."""
    with HttpClient(clock=FixedClock()) as client:
        yield client


def _account(
    label: str,
    provider_id: ProviderId,
    *,
    expiry: Expiry | None = None,
    plan: str = "team",
) -> Account:
    if provider_id is ProviderId.CLAUDE:
        access_expiry = expiry or KnownExpiry(
            REFERENCE_TIME + timedelta(hours=1)
        )
        if not isinstance(access_expiry, KnownExpiry):
            raise ValueError("Claude login expiry must be known.")
        credentials = ClaudeLoginCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            access_expiry=access_expiry,
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        )
    else:
        credentials = CodexCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            expiry=expiry or UnknownExpiry(),
            account_id=f"acct_{label}",
        )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan=plan,
    )


def _report(*, plan: str = "team") -> UsageReport:
    return UsageReport(
        windows=(UsageWindow("5h", 0.25, None),),
        plan=plan,
    )


def _service(
    store: InMemoryAccountStore,
    http: HttpClient,
    *providers: ScriptedProvider,
    refresher: CredentialCoordinator | None = None,
    clock: FixedClock | None = None,
    resolver: CredentialResolver | None = None,
) -> UsageCheckService:
    credential_refresher = refresher or ScriptedCredentialCoordinator(store)
    return UsageCheckService(
        store,
        http,
        {provider.id: provider for provider in providers},
        credential_refresher,
        clock=clock or FixedClock(),
        resolver=resolver,
    )


def test_filter_selects_store_accounts_and_returns_immutable_results(
    http: HttpClient,
) -> None:
    accounts = (
        _account("codex-one", ProviderId.CODEX),
        _account("claude", ProviderId.CLAUDE),
        _account("codex-two", ProviderId.CODEX),
    )
    store = InMemoryAccountStore(accounts)
    codex = ScriptedProvider(
        ProviderId.CODEX,
        {"codex-one": [_report()], "codex-two": [_report()]},
    )
    clock = FixedClock()
    resolver = RecordingCredentialResolver(store)

    result = _service(
        store,
        http,
        codex,
        clock=clock,
        resolver=resolver,
    ).check(ProviderId.CODEX)

    assert [usage.label for usage in result.usages] == [
        "codex-one",
        "codex-two",
    ]
    assert result.failures == ()
    assert store.filters == [ProviderId.CODEX]
    assert store.iterations == 0
    assert result.reference_time == REFERENCE_TIME
    assert clock.calls == 1
    assert AccountUsage.__dataclass_params__.frozen is True
    assert resolver.events == [
        "open:codex-one",
        "close:codex-one",
        "open:codex-two",
        "close:codex-two",
    ]
    assert "test-only-codex-one-access" not in repr(result)


def test_partial_success_keeps_usage_and_typed_failure(
    http: HttpClient,
) -> None:
    claude_account = _account("claude", ProviderId.CLAUDE)
    codex_account = _account("codex", ProviderId.CODEX)
    store = InMemoryAccountStore((claude_account, codex_account))
    claude = ScriptedProvider(
        ProviderId.CLAUDE,
        {"claude": [_report()]},
    )
    codex = ScriptedProvider(
        ProviderId.CODEX,
        {"codex": [TransientError("provider temporarily unavailable")]},
    )

    result = _service(store, http, claude, codex).check()

    assert result.usages == (
        AccountUsage(
            label=AccountLabel("claude"),
            provider_id=ProviderId.CLAUDE,
            plan="team",
            report=_report(),
        ),
    )
    assert len(result.failures) == 1
    assert isinstance(result.failures[0], TransientFailure)
    assert result.failures[0].message == "provider temporarily unavailable"


@pytest.mark.parametrize(
    ("provider_id", "expires_in", "refreshes"),
    [
        (ProviderId.CLAUDE, timedelta(0), True),
        (ProviderId.CODEX, timedelta(seconds=60), True),
        (ProviderId.CODEX, timedelta(seconds=61), False),
    ],
)
def test_usage_expiry_policy_refreshes_before_the_first_fetch(
    http: HttpClient,
    provider_id: ProviderId,
    expires_in: timedelta,
    refreshes: bool,
) -> None:
    account = _account(
        "selected",
        provider_id,
        expiry=KnownExpiry(REFERENCE_TIME + expires_in),
    )
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        provider_id,
        {"selected": [_report()]},
    )
    refresher = ScriptedCredentialCoordinator(store)

    result = _service(
        store,
        http,
        provider,
        refresher=refresher,
    ).check()

    assert len(result.usages) == 1
    assert provider.events == ["fetch:selected"]
    assert refresher.calls == (["selected"] if refreshes else [])
    assert provider.fetch_tokens == [
        (
            "test-only-selected-refreshed"
            if refreshes
            else "test-only-selected-access"
        )
    ]


def test_invalid_expiry_and_missing_adapter_fail_without_provider_traffic(
    http: HttpClient,
) -> None:
    invalid = _account("invalid", ProviderId.CODEX, expiry=InvalidExpiry())
    missing = _account("missing", ProviderId.CLAUDE)
    store = InMemoryAccountStore((invalid, missing))
    codex = ScriptedProvider(
        ProviderId.CODEX,
        {"invalid": [_report()]},
    )

    result = _service(store, http, codex).check()

    assert isinstance(result.failures[0], InvalidExpiryFailure)
    assert isinstance(result.failures[1], UnknownProviderFailure)
    assert result.usages == ()
    assert codex.events == []
    assert store.persisted == []


def test_authentication_refreshes_durably_then_retries_once(
    http: HttpClient,
) -> None:
    account = _account("account", ProviderId.CLAUDE)
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        ProviderId.CLAUDE,
        {"account": [AuthError("expired"), _report()]},
    )
    refresher = ScriptedCredentialCoordinator(store)

    result = _service(
        store,
        http,
        provider,
        refresher=refresher,
    ).check()

    assert len(result.usages) == 1
    assert result.failures == ()
    assert provider.events == [
        "fetch:account",
        "fetch:account",
    ]
    assert refresher.calls == ["account"]
    assert provider.fetch_tokens == [
        "test-only-account-access",
        "test-only-account-refreshed",
    ]
    assert len(store.persisted) == 1
    assert store.saved("account").access_token.endswith("-refreshed")


@pytest.mark.parametrize(
    ("steps", "refresh_rejected", "kind"),
    [
        (
            [AuthError("expired")],
            True,
            FetchFailureKind.REFRESH_REJECTED,
        ),
        (
            [
                ForbiddenError(
                    "forbidden",
                    api_message="profile scope denied",
                    required_scope="different:scope",
                )
            ],
            False,
            FetchFailureKind.FORBIDDEN,
        ),
        (
            [
                RateLimitError(
                    "rate limited",
                    retry_after=_RETRY_AFTER_SECONDS,
                )
            ],
            False,
            FetchFailureKind.RATE_LIMITED,
        ),
    ],
)
def test_terminal_failures_preserve_typed_recovery_metadata(
    http: HttpClient,
    steps: list[FetchStep],
    refresh_rejected: bool,
    kind: FetchFailureKind,
) -> None:
    account = _account("account", ProviderId.CLAUDE)
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        ProviderId.CLAUDE,
        {"account": steps},
    )
    refresh_failure = ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=ProviderFailureKind.REJECTED,
        message="Test refresh rejected.",
    )
    refresher = ScriptedCredentialCoordinator(
        store,
        (refresh_failure,) if refresh_rejected else (),
    )

    failure = (
        _service(
            store,
            http,
            provider,
            refresher=refresher,
        )
        .check()
        .failures[0]
    )

    assert failure.kind is kind
    if isinstance(failure, RefreshRejectedFailure):
        assert failure.message == "Test refresh rejected."
        assert failure.provider_failure is not None
        assert failure.provider_failure.kind is ProviderFailureKind.REJECTED
        assert len(store.persisted) == 1
    elif isinstance(failure, ForbiddenFailure):
        assert failure.message == "profile scope denied"
        assert failure.required_scope == "different:scope"
    elif isinstance(failure, RateLimitFailure):
        assert failure.retry_after_seconds == _RETRY_AFTER_SECONDS
    else:
        raise AssertionError(f"Unexpected failure type: {type(failure)}")


def test_provider_payload_failure_preserves_only_safe_boundary_fields(
    http: HttpClient,
) -> None:
    safe = ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=ProviderFailureKind.MALFORMED,
        message="Codex usage response is malformed.",
        fields=("rate_limit.primary_window.used_percent",),
    )
    store = InMemoryAccountStore((_account("codex", ProviderId.CODEX),))
    provider = ScriptedProvider(
        ProviderId.CODEX,
        {"codex": [ProviderBoundaryError(safe)]},
    )

    failure = _service(store, http, provider).check().failures[0]

    assert isinstance(failure, ProviderPayloadFailure)
    assert failure.provider_failure == safe
    assert failure.provider_failure.fields == (
        "rate_limit.primary_window.used_percent",
    )


def test_refresh_boundary_failure_preserves_only_safe_metadata(
    http: HttpClient,
) -> None:
    rejected_input = "test-only-rejected-raw-refresh-token"
    account = _account("account", ProviderId.CODEX)
    account.credentials = replace(
        account.credentials,
        refresh_token=rejected_input,
    )
    safe = ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=ProviderFailureKind.MALFORMED,
        message="Codex refresh response is malformed.",
        fields=("tokens.access_token",),
    )
    store = InMemoryAccountStore((account,))

    outcome = TokenMaintenanceService(
        store,
        ScriptedCredentialCoordinator(store, (safe,)),
        clock=FixedClock(),
    ).refresh_account(account, force=True)

    failure = outcome.provider_failure
    assert failure is not None
    assert failure == safe
    assert failure.kind is ProviderFailureKind.MALFORMED
    assert failure.fields == ("tokens.access_token",)
    assert rejected_input not in repr(outcome)
    assert store.saved("account").last_refresh_error == safe.message


@pytest.mark.parametrize(
    "error",
    [TransientError("test transient"), ReplaceFailedError()],
)
def test_operational_refresh_failures_never_become_auth_rejection(
    error: UsageError,
) -> None:
    account = _account("account", ProviderId.CLAUDE)
    store = InMemoryAccountStore((account,))
    outcome = TokenMaintenanceService(
        store,
        ScriptedCredentialCoordinator(store, (error,)),
        clock=FixedClock(),
    ).refresh_account(account, force=True)

    assert outcome.status is RefreshStatus.FAILED
    assert outcome.exit_code is ExitCode.SYSTEM_ERROR
    assert outcome.provider_failure is None
    assert store.saved("account").access_token.endswith("-access")
    if isinstance(error, PersistenceError):
        assert outcome.persistence_error is error
        assert store.persisted == []
        assert store.saved("account").last_refresh_status is None
    else:
        assert isinstance(outcome.operational_error, TransientError)
        assert len(store.persisted) == 1
        assert (
            store.saved("account").last_refresh_status is RefreshStatus.FAILED
        )


def test_plan_and_account_identity_change_in_one_atomic_persist(
    http: HttpClient,
) -> None:
    account = _account(
        "codex",
        ProviderId.CODEX,
        plan="unknown",
    )
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        ProviderId.CODEX,
        {"codex": [_report(plan="pro")]},
        account_id_on_fetch="acct_discovered",
    )

    result = _service(store, http, provider).check()

    assert result.usages[0].plan == "pro"
    assert len(store.persisted) == 1
    saved = store.saved("codex")
    assert saved.plan == "pro"
    assert saved.provider_account_id == "acct_discovered"


def test_persistence_failure_cannot_be_presented_as_successful_usage(
    http: HttpClient,
) -> None:
    account = _account(
        "codex",
        ProviderId.CODEX,
        plan="unknown",
    )
    store = InMemoryAccountStore(
        (account,),
        persist_error=ReplaceFailedError(),
    )
    provider = ScriptedProvider(
        ProviderId.CODEX,
        {"codex": [_report(plan="pro")]},
    )

    result = _service(store, http, provider).check()

    assert result.usages == ()
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert isinstance(failure, PersistenceFailure)
    assert failure.persistence_code is PersistenceCode.REPLACE_FAILED
    assert store.saved("codex").plan == "unknown"
