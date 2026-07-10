"""Load-bearing behavior tests for typed usage orchestration."""

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    ReplaceFailedError,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.usage import (
    AccountUsage,
    FetchFailureKind,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PersistenceFailure,
    RateLimitFailure,
    RefreshRejectedFailure,
    TransientFailure,
    UnknownProviderFailure,
    UsageCheckService,
)
from tests.test_support import REFERENCE_TIME, FixedClock

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

    def persist(self, account: Account) -> None:
        if self.persist_error is not None:
            raise self.persist_error
        saved = _copy_account(account)
        self._saved[str(saved.label)] = saved
        self.persisted.append(_copy_account(saved))

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
        refresh_ok: bool = True,
        account_id_on_fetch: str | None = None,
    ) -> None:
        self.id = provider_id
        self.fetch_steps = fetch_steps
        self.refresh_ok = refresh_ok
        self.account_id_on_fetch = account_id_on_fetch
        self.events: list[str] = []
        self.fetch_tokens: list[str] = []
        self.fetch_scopes: list[tuple[str, ...] | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> DetectedCredentials | None:
        del credential_home
        return None

    def fetch_usage(
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

    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        del http
        self.events.append(f"refresh:{account.label}")
        if not self.refresh_ok:
            return False
        account.credentials = replace(
            account.credentials,
            access_token=f"test-only-{account.label}-refreshed",
            expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        )
        return True

    def run_setup_token(self) -> str | None:
        return None


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
    scopes: tuple[str, ...] | None = None,
) -> Account:
    credentials = (
        ClaudeCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            expiry=expiry or UnknownExpiry(),
            scopes=scopes,
        )
        if provider_id is ProviderId.CLAUDE
        else CodexCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            expiry=expiry or UnknownExpiry(),
            account_id=f"acct_{label}",
        )
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
) -> UsageCheckService:
    return UsageCheckService(
        store,
        http,
        {provider.id: provider for provider in providers},
        clock=FixedClock(),
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

    result = _service(store, http, codex).check(ProviderId.CODEX)

    assert [usage.label for usage in result.usages] == [
        "codex-one",
        "codex-two",
    ]
    assert result.failures == ()
    assert store.filters == [ProviderId.CODEX]
    assert store.iterations == 0
    assert AccountUsage.__dataclass_params__.frozen is True


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

    result = _service(store, http, provider).check()

    assert len(result.usages) == 1
    assert provider.events == (
        ["refresh:selected", "fetch:selected"]
        if refreshes
        else ["fetch:selected"]
    )


def test_invalid_expiry_and_missing_adapter_fail_without_provider_traffic(
    http: HttpClient,
) -> None:
    invalid = _account(
        "invalid",
        ProviderId.CLAUDE,
        expiry=InvalidExpiry(),
    )
    missing = _account("missing", ProviderId.CODEX)
    store = InMemoryAccountStore((invalid, missing))
    claude = ScriptedProvider(
        ProviderId.CLAUDE,
        {"invalid": [_report()]},
    )

    result = _service(store, http, claude).check()

    assert isinstance(result.failures[0], InvalidExpiryFailure)
    assert isinstance(result.failures[1], UnknownProviderFailure)
    assert result.usages == ()
    assert claude.events == []
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

    result = _service(store, http, provider).check()

    assert len(result.usages) == 1
    assert result.failures == ()
    assert provider.events == [
        "fetch:account",
        "refresh:account",
        "fetch:account",
    ]
    assert provider.fetch_tokens == [
        "test-only-account-access",
        "test-only-account-refreshed",
    ]
    assert len(store.persisted) == 1
    assert store.saved("account").access_token.endswith("-refreshed")


@pytest.mark.parametrize(
    ("steps", "refresh_ok", "kind"),
    [
        (
            [AuthError("expired")],
            False,
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
            True,
            FetchFailureKind.FORBIDDEN,
        ),
        (
            [
                RateLimitError(
                    "rate limited",
                    retry_after=_RETRY_AFTER_SECONDS,
                )
            ],
            True,
            FetchFailureKind.RATE_LIMITED,
        ),
    ],
)
def test_terminal_failures_preserve_typed_recovery_metadata(
    http: HttpClient,
    steps: list[FetchStep],
    refresh_ok: bool,
    kind: FetchFailureKind,
) -> None:
    account = _account("account", ProviderId.CLAUDE)
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        ProviderId.CLAUDE,
        {"account": steps},
        refresh_ok=refresh_ok,
    )

    failure = _service(store, http, provider).check().failures[0]

    assert failure.kind is kind
    if isinstance(failure, RefreshRejectedFailure):
        assert failure.message == "Refresh token unavailable or rejected."
        assert len(store.persisted) == 1
    elif isinstance(failure, ForbiddenFailure):
        assert failure.message == "profile scope denied"
        assert failure.required_scope == "different:scope"
    elif isinstance(failure, RateLimitFailure):
        assert failure.retry_after_seconds == _RETRY_AFTER_SECONDS
    else:
        raise AssertionError(f"Unexpected failure type: {type(failure)}")


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


def test_canonical_claude_forbidden_switches_route_and_retries_once(
    http: HttpClient,
) -> None:
    account = _account("claude", ProviderId.CLAUDE, scopes=None)
    store = InMemoryAccountStore((account,))
    provider = ScriptedProvider(
        ProviderId.CLAUDE,
        {
            "claude": [
                ForbiddenError(
                    "forbidden",
                    required_scope="user:profile",
                ),
                _report(),
            ]
        },
    )

    result = _service(store, http, provider).check()

    assert len(result.usages) == 1
    assert provider.fetch_scopes == [None, ()]
    assert len(store.persisted) == 1
    assert store.saved("claude").scopes == ()
