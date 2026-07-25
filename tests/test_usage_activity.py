"""Load-bearing service tests for scoped token activity."""

import re
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    AccountTokenActivitySnapshot,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    TokenActivityReading,
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.credentials.authorities import AuthenticatedSavedAccount
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.errors import (
    ProviderIdentityError,
    TransientError,
    UsageError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import (
    ActivitySnapshotError,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
)
from sidekick_usages.usage.activity import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
)
from sidekick_usages.usage.models import (
    CompleteTokenActivity,
    PartialTokenActivity,
    RefreshRejectedFailure,
    TokenActivityFailureKind,
    TransientFailure,
    UnavailableTokenActivity,
    activity_has_failure,
)
from sidekick_usages.usage.ports import (
    AccountTokenActivitySnapshots,
    UsagePersistence,
)
from sidekick_usages.usage.service import UsageCheckService
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    RuntimeCredentialResolver,
    make_account_store_with_private,
)

type FetchStep = UsageReport | UsageError
type ActivityStep = TokenActivityReading | UsageError

_CLAUDE_TOTAL = 903_464_085
_CODEX_TOTAL = 7_449_473_297


class _ScriptedProvider(Provider):
    """Return usage steps without crossing a provider boundary."""

    display_name = "Synthetic provider"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        provider_id: ProviderId,
        steps: Mapping[str, FetchStep],
        discovered_account_ids: Mapping[str, str] | None = None,
    ) -> None:
        self.id = provider_id
        self.steps = dict(steps)
        self.discovered_account_ids = (
            {}
            if discovered_account_ids is None
            else dict(discovered_account_ids)
        )

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return self._unsupported()

    def credentials_from_token(self, token: str) -> CredentialDetection:
        del token
        return self._unsupported()

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del http
        label = str(account.label)
        if account_id := self.discovered_account_ids.get(label):
            credentials = account.credentials
            if not isinstance(credentials, CodexCredentials):
                raise AssertionError("Discovered account id requires Codex.")
            account.credentials = replace(
                credentials,
                account_id=account_id,
            )
        step = self.steps[label]
        if isinstance(step, UsageError):
            raise step
        return step

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del account, http
        return self._unsupported()

    def _unsupported(self) -> ProviderFailure:
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.UNSUPPORTED,
            message="Synthetic provider operation is unsupported.",
        )


class _LocalActivity(LocalTokenActivitySource):
    """Record one provider-local collection call."""

    provider_id = ProviderId.CLAUDE

    def __init__(self, reading: TokenActivityReading) -> None:
        self.reading = reading
        self.calls = 0

    def read(self, reference_time: datetime) -> TokenActivityReading:
        assert reference_time == REFERENCE_TIME
        self.calls += 1
        return self.reading


class _AccountActivity(AccountTokenActivitySource):
    """Return scripted account profiles in request order."""

    provider_id = ProviderId.CODEX

    def __init__(self, steps: Mapping[str, ActivityStep]) -> None:
        self.steps = dict(steps)
        self.calls: list[AccountLabel] = []
        self.account_ids: list[str | None] = []

    def read(
        self,
        account: AuthenticatedSavedAccount,
        http: HttpClient,
    ) -> TokenActivityReading:
        del http
        runtime = account.lease.account
        self.calls.append(runtime.label)
        self.account_ids.append(runtime.provider_account_id)
        step = self.steps[str(runtime.label)]
        if isinstance(step, UsageError):
            raise step
        return step


class _ActivitySnapshots(AccountTokenActivitySnapshots):
    """Retain scripted snapshots without crossing the filesystem boundary."""

    def __init__(
        self,
        snapshots: tuple[AccountTokenActivitySnapshot, ...] = (),
        *,
        save_error: ActivitySnapshotError | None = None,
    ) -> None:
        self.snapshots = {
            snapshot.provider_account_id: snapshot for snapshot in snapshots
        }
        self.save_error = save_error
        self.loads: list[AccountLabel] = []
        self.saves: list[AccountTokenActivitySnapshot] = []

    def load(
        self,
        account: SavedAccount,
    ) -> AccountTokenActivitySnapshot | None:
        self.loads.append(account.label)
        identity = account.provider_identity
        return None if identity is None else self.snapshots.get(str(identity))

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        self.saves.append(snapshot)
        if self.save_error is not None:
            raise self.save_error
        self.snapshots[snapshot.provider_account_id] = snapshot
        return snapshot


@pytest.fixture
def http() -> Iterator[HttpClient]:
    """Yield an idle HTTP facade for injected provider fakes."""
    with HttpClient(clock=FixedClock()) as client:
        yield client


def _account(
    label: str,
    provider_id: ProviderId,
    *,
    expiry: Expiry | None = None,
) -> Account:
    credentials = (
        ClaudeSetupTokenCredentials(access_token=f"test-only-{label}-access")
        if provider_id is ProviderId.CLAUDE
        else CodexCredentials(
            access_token=f"test-only-{label}-access",
            expiry=expiry or UnknownExpiry(),
            account_id=f"acct_{label}",
        )
    )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="team",
    )


def _report() -> UsageReport:
    return UsageReport(
        windows=(UsageWindow("5h", 0.25, None),),
        plan="team",
    )


def _summary(
    total: int,
    scope: TokenActivityScope,
    since: date | None = None,
) -> TokenActivitySummary:
    return TokenActivitySummary(
        total_tokens=total,
        scope=scope,
        since=since,
    )


def _snapshot(
    account: Account,
    total: int,
    since: date,
) -> AccountTokenActivitySnapshot:
    assert account.provider_account_id is not None
    return AccountTokenActivitySnapshot(
        provider_id=account.provider_id,
        provider_account_id=account.provider_account_id,
        summary=_summary(total, TokenActivityScope.ACCOUNT, since),
        fetched_at=REFERENCE_TIME - timedelta(hours=1),
    )


def _service(
    tmp_path: Path,
    http: HttpClient,
    accounts: tuple[Account, ...],
    providers: tuple[_ScriptedProvider, ...],
    *,
    local_activity: LocalTokenActivitySource | None = None,
    account_activity: AccountTokenActivitySource | None = None,
    activity_snapshots: AccountTokenActivitySnapshots | None = None,
) -> tuple[UsageCheckService, AccountStore]:
    store, private = make_account_store_with_private(tmp_path, accounts)
    registry: dict[ProviderId, Provider] = {
        provider.id: provider for provider in providers
    }
    credentials = CredentialService(
        store,
        http,
        registry,
        private,
        clock=FixedClock(),
    )
    return (
        UsageCheckService(
            store,
            http,
            registry,
            credentials,
            clock=FixedClock(),
            local_activity_sources=(
                {}
                if local_activity is None
                else {ProviderId.CLAUDE: local_activity}
            ),
            account_activity_sources=(
                {}
                if account_activity is None
                else {ProviderId.CODEX: account_activity}
            ),
            persistence=UsagePersistence(activity=activity_snapshots),
            resolver=RuntimeCredentialResolver(store),
        ),
        store,
    )


def test_collection_preserves_scope_and_is_independent_of_usage_rows(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    accounts = (
        _account("claude-one", ProviderId.CLAUDE),
        _account("claude-two", ProviderId.CLAUDE),
        _account("codex-one", ProviderId.CODEX),
        _account("codex-two", ProviderId.CODEX),
    )
    claude = _ScriptedProvider(
        ProviderId.CLAUDE,
        {"claude-one": _report(), "claude-two": _report()},
    )
    codex_two = accounts[-1]
    assert isinstance(codex_two.credentials, CodexCredentials)
    codex_two.credentials = replace(codex_two.credentials, account_id=None)
    codex = _ScriptedProvider(
        ProviderId.CODEX,
        {
            "codex-one": _report(),
            "codex-two": TransientError("rate-limit endpoint failed"),
        },
        discovered_account_ids={"codex-two": "acct_discovered"},
    )
    local = _LocalActivity(
        _summary(_CLAUDE_TOTAL, TokenActivityScope.LOCAL_INSTALLATION)
    )
    profiles = _AccountActivity(
        {
            "codex-one": _summary(
                4_000_000_000,
                TokenActivityScope.ACCOUNT,
            ),
            "codex-two": _summary(
                _CODEX_TOTAL - 4_000_000_000,
                TokenActivityScope.ACCOUNT,
            ),
        }
    )

    service, store = _service(
        tmp_path,
        http,
        accounts,
        (claude, codex),
        local_activity=local,
        account_activity=profiles,
    )
    result = service.check()

    assert [usage.label for usage in result.usages] == [
        "claude-one",
        "claude-two",
        "codex-one",
    ]
    assert isinstance(result.failures[0], TransientFailure)
    assert local.calls == 1
    assert profiles.calls == ["codex-one", "codex-two"]
    assert profiles.account_ids == ["acct_codex-one", "acct_discovered"]
    saved = store.get("codex-two")
    assert saved is not None
    assert saved.provider_account_id == "acct_discovered"
    assert result.activities == (
        CompleteTokenActivity(
            provider_id=ProviderId.CLAUDE,
            summary=_summary(
                _CLAUDE_TOTAL,
                TokenActivityScope.LOCAL_INSTALLATION,
            ),
        ),
        CompleteTokenActivity(
            provider_id=ProviderId.CODEX,
            summary=_summary(_CODEX_TOTAL, TokenActivityScope.ACCOUNT),
        ),
    )


def test_account_activity_reports_known_coverage_and_attempt_failures(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    accounts = (
        _account("known", ProviderId.CODEX),
        _account(
            "ineligible",
            ProviderId.CODEX,
            expiry=KnownExpiry(
                REFERENCE_TIME.replace(microsecond=0) + timedelta(seconds=30)
            ),
        ),
        _account("profile-failed", ProviderId.CODEX),
    )
    codex = _ScriptedProvider(
        ProviderId.CODEX,
        {
            "known": _report(),
            "ineligible": _report(),
            "profile-failed": _report(),
        },
    )
    profiles = _AccountActivity(
        {
            "known": _summary(_CODEX_TOTAL, TokenActivityScope.ACCOUNT),
            "profile-failed": TransientError("secret provider detail"),
        }
    )

    service, _store = _service(
        tmp_path,
        http,
        accounts,
        (codex,),
        account_activity=profiles,
    )
    result = service.check()

    assert [usage.label for usage in result.usages] == [
        "known",
        "profile-failed",
    ]
    assert isinstance(result.failures[0], RefreshRejectedFailure)
    assert profiles.calls == ["known", "profile-failed"]
    outcome = result.activities[0]
    assert isinstance(outcome, PartialTokenActivity)
    assert outcome.summary.total_tokens == _CODEX_TOTAL
    assert (outcome.covered_accounts, outcome.saved_accounts) == (1, 3)
    assert len(outcome.issues) == 1
    assert outcome.issues[0].label == "profile-failed"
    assert outcome.issues[0].kind is TokenActivityFailureKind.TRANSIENT
    assert "secret provider detail" not in outcome.issues[0].message


def test_missing_provider_identity_is_not_activity_eligible(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    account = _account("missing-id", ProviderId.CODEX)
    assert isinstance(account.credentials, CodexCredentials)
    account.credentials = replace(account.credentials, account_id=None)
    provider = _ScriptedProvider(
        ProviderId.CODEX,
        {
            "missing-id": ProviderIdentityError(
                "Missing Codex account id; log in again."
            )
        },
    )
    profiles = _AccountActivity({})

    service, _store = _service(
        tmp_path,
        http,
        (account,),
        (provider,),
        account_activity=profiles,
    )
    result = service.check()

    assert result.failures[0].message == (
        "Missing Codex account id; log in again."
    )
    assert profiles.calls == []
    assert isinstance(result.activities[0], UnavailableTokenActivity)


def test_fresh_and_retained_account_snapshots_form_one_complete_total(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    """An auth-ineligible account keeps its last authoritative activity."""
    fresh = _account("fresh", ProviderId.CODEX)
    retained = _account(
        "retained",
        ProviderId.CODEX,
        expiry=KnownExpiry(
            REFERENCE_TIME.replace(microsecond=0) + timedelta(seconds=30)
        ),
    )
    provider = _ScriptedProvider(
        ProviderId.CODEX,
        {"fresh": _report(), "retained": _report()},
    )
    profiles = _AccountActivity(
        {
            "fresh": _summary(
                7_449_473_297,
                TokenActivityScope.ACCOUNT,
                date(2026, 4, 7),
            )
        }
    )
    snapshots = _ActivitySnapshots(
        (_snapshot(retained, 900_000_000, date(2026, 3, 30)),)
    )
    service, _store = _service(
        tmp_path,
        http,
        (fresh, retained),
        (provider,),
        account_activity=profiles,
        activity_snapshots=snapshots,
    )

    result = service.check()

    assert isinstance(result.failures[0], RefreshRejectedFailure)
    assert profiles.calls == ["fresh"]
    assert snapshots.loads == ["retained"]
    assert len(snapshots.saves) == 1
    outcome = result.activities[0]
    assert outcome == CompleteTokenActivity(
        provider_id=ProviderId.CODEX,
        summary=_summary(
            8_349_473_297,
            TokenActivityScope.ACCOUNT,
            date(2026, 3, 30),
        ),
    )


def test_profile_failure_uses_snapshot_and_preserves_activity_warning(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    """A failed refresh does not erase the last successful profile."""
    account = _account("account", ProviderId.CODEX)
    provider = _ScriptedProvider(
        ProviderId.CODEX,
        {"account": _report()},
    )
    profiles = _AccountActivity(
        {"account": TransientError("secret provider detail")}
    )
    snapshots = _ActivitySnapshots(
        (_snapshot(account, _CODEX_TOTAL, date(2026, 4, 7)),)
    )
    service, _store = _service(
        tmp_path,
        http,
        (account,),
        (provider,),
        account_activity=profiles,
        activity_snapshots=snapshots,
    )

    outcome = service.check().activities[0]

    assert isinstance(outcome, CompleteTokenActivity)
    assert outcome.summary.total_tokens == _CODEX_TOTAL
    assert outcome.summary.since == date(2026, 4, 7)
    assert outcome.issues[0].kind is TokenActivityFailureKind.TRANSIENT
    assert activity_has_failure(outcome)


def test_snapshot_write_failure_keeps_fresh_total_and_fails_explicitly(
    tmp_path: Path,
    http: HttpClient,
) -> None:
    """Durability failure cannot suppress a valid current provider value."""
    account = _account("account", ProviderId.CODEX)
    provider = _ScriptedProvider(
        ProviderId.CODEX,
        {"account": _report()},
    )
    fresh = _summary(
        _CODEX_TOTAL,
        TokenActivityScope.ACCOUNT,
        date(2026, 4, 7),
    )
    snapshots = _ActivitySnapshots(
        save_error=ActivitySnapshotError(ActivitySnapshotFailureKind.WRITE)
    )
    service, _store = _service(
        tmp_path,
        http,
        (account,),
        (provider,),
        account_activity=_AccountActivity({"account": fresh}),
        activity_snapshots=snapshots,
    )

    outcome = service.check().activities[0]

    assert isinstance(outcome, CompleteTokenActivity)
    assert outcome.summary == fresh
    assert outcome.issues[0].kind is TokenActivityFailureKind.PERSISTENCE
    assert activity_has_failure(outcome)
