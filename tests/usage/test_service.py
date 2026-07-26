"""Load-bearing behavior tests for typed usage orchestration."""

import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest

from sidekick_usages.core.expiry import (
    Expiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    CodexCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.authorities import (
    AuthorizedCredentialResolver,
)
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import (
    CredentialRefresher,
    TokenMaintenanceService,
)
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
from sidekick_usages.usage.lookup.models import AccountLookupCompletion
from sidekick_usages.usage.lookup.service import AccountCredentialAccess
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailureKind,
    ForbiddenFailure,
    MetricsFreshness,
    PersistenceFailure,
    ProviderPayloadFailure,
    RateLimitFailure,
    RefreshRejectedFailure,
    TransientFailure,
    UnknownProviderFailure,
)
from sidekick_usages.usage.service import UsageCheckService
from tests.fakes.usage import (
    InMemoryAccountStore,
    InMemoryOperationLocks,
    ScriptedCredentialCoordinator,
)
from tests.support.accounts import RuntimeCredentialResolver, saved_account
from tests.support.time import REFERENCE_TIME, FixedClock

type FetchStep = UsageReport | UsageError
type FetchGate = Callable[[Account], None]

_RETRY_AFTER_SECONDS = 17
_CONCURRENCY_TIMEOUT_SECONDS = 5.0
_EXPECTED_LEASE_EVENTS = 4


class ScriptedProvider(Provider):
    """Provider double exposing fetch, refresh, and mutation events."""

    display_name = "Test provider"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        provider_id: ProviderId,
        fetch_steps: dict[str, list[FetchStep]],
        *,
        account_ids_on_fetch: dict[str, str] | None = None,
        fetch_gate: FetchGate | None = None,
    ) -> None:
        self.id = provider_id
        self.fetch_steps = fetch_steps
        self.account_ids_on_fetch = account_ids_on_fetch or {}
        self.fetch_gate = fetch_gate
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
        if self.fetch_gate is not None:
            self.fetch_gate(account)
        self.events.append(f"fetch:{label}")
        self.fetch_tokens.append(account.access_token)
        self.fetch_scopes.append(account.scopes)
        if label in self.account_ids_on_fetch:
            credentials = account.credentials
            if not isinstance(credentials, CodexCredentials):
                raise AssertionError("Account-id mutation requires Codex.")
            account.credentials = replace(
                credentials,
                account_id=self.account_ids_on_fetch[label],
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
    refresher: CredentialRefresher | None = None,
    clock: FixedClock | None = None,
    resolver: AuthorizedCredentialResolver | None = None,
    operation_locks: InMemoryOperationLocks | None = None,
) -> UsageCheckService:
    credential_refresher = refresher or ScriptedCredentialCoordinator(store)
    credential_resolver = resolver or RuntimeCredentialResolver(store)
    return UsageCheckService(
        store,
        http,
        {provider.id: provider for provider in providers},
        credential_refresher,
        clock=clock or FixedClock(),
        credential_access=AccountCredentialAccess(
            credential_resolver,
            (
                InMemoryOperationLocks()
                if operation_locks is None
                else operation_locks
            ),
        ),
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
    started = Barrier(2)
    release = Event()
    blocked_waiting = Event()

    def coordinate(account: Account) -> None:
        blocked = str(account.label) == "codex-one"
        if blocked:
            blocked_waiting.set()
        started.wait(timeout=_CONCURRENCY_TIMEOUT_SECONDS)
        if blocked:
            assert release.wait(_CONCURRENCY_TIMEOUT_SECONDS)

    codex = ScriptedProvider(
        ProviderId.CODEX,
        {"codex-one": [_report()], "codex-two": [_report()]},
        fetch_gate=coordinate,
    )
    clock = FixedClock()
    resolver = RuntimeCredentialResolver(store)
    operation_locks = InMemoryOperationLocks()
    completion_order: list[AccountLabel] = []

    def observe(completion: AccountLookupCompletion) -> None:
        completion_order.append(completion.label)
        if completion.label == "codex-two":
            assert blocked_waiting.is_set()
            release.set()

    result = _service(
        store,
        http,
        codex,
        clock=clock,
        resolver=resolver,
        operation_locks=operation_locks,
    ).check(ProviderId.CODEX, observe=observe)

    assert [usage.label for usage in result.usages] == [
        "codex-one",
        "codex-two",
    ]
    assert completion_order == ["codex-two", "codex-one"]
    assert result.failures == ()
    assert store.filters == [ProviderId.CODEX]
    assert store.iterations == 0
    assert result.reference_time == REFERENCE_TIME
    assert clock.calls == 1
    assert AccountUsage.__dataclass_params__.frozen is True
    assert set(resolver.events[:2]) == {
        "open:codex-one",
        "open:codex-two",
    }
    assert len(resolver.events) == _EXPECTED_LEASE_EVENTS
    assert resolver.events.count("open:codex-one") == 1
    assert resolver.events.count("open:codex-two") == 1
    assert resolver.events.count("close:codex-one") == 1
    assert resolver.events.count("close:codex-two") == 1
    assert set(operation_locks.entries) == {
        saved_account(accounts[0]).account_id,
        saved_account(accounts[2]).account_id,
    }
    assert len(operation_locks.entries) == len(result.usages)
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
            account_id=saved_account(claude_account).account_id,
            label=AccountLabel("claude"),
            provider_id=ProviderId.CLAUDE,
            plan="team",
            report=_report(),
            fetched_at=REFERENCE_TIME,
            freshness=MetricsFreshness.CURRENT,
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


def test_missing_adapter_fails_without_provider_traffic(
    http: HttpClient,
) -> None:
    missing = _account("missing", ProviderId.CLAUDE)
    store = InMemoryAccountStore((missing,))
    codex = ScriptedProvider(
        ProviderId.CODEX,
        {},
    )

    result = _service(store, http, codex).check()

    assert isinstance(result.failures[0], UnknownProviderFailure)
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
    ).refresh_account(saved_account(account), force=True)

    failure = outcome.provider_failure
    assert failure is not None
    assert failure == safe
    assert failure.kind is ProviderFailureKind.MALFORMED
    assert failure.fields == ("tokens.access_token",)
    assert rejected_input not in repr(outcome)


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
    ).refresh_account(saved_account(account), force=True)

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


def test_owner_persists_plan_and_rejects_credential_mutation(
    http: HttpClient,
) -> None:
    plan_account = _account(
        "plan",
        ProviderId.CODEX,
        plan="unknown",
    )
    identity_account = _account("identity", ProviderId.CODEX)
    store = InMemoryAccountStore((plan_account, identity_account))
    provider = ScriptedProvider(
        ProviderId.CODEX,
        {
            "plan": [_report(plan="pro")],
            "identity": [_report()],
        },
        account_ids_on_fetch={"identity": "acct_discovered"},
    )

    result = _service(store, http, provider).check()

    assert result.usages[0].plan == "pro"
    assert isinstance(result.failures[0], ProviderPayloadFailure)
    assert (
        result.failures[0].provider_failure.kind
        is ProviderFailureKind.IDENTITY_MISMATCH
    )
    assert len(store.persisted) == 1
    saved = store.saved("plan")
    assert saved.plan == "pro"
    assert store.saved("identity").provider_account_id == "acct_identity"


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
