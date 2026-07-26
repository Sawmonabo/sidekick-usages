"""Typed credential-refresh test boundaries and builders."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event, Lock

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshCrashPoint,
    CredentialRefreshTransactions,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    CredentialStageReader,
    Provider,
    ProviderAuthenticatedAccount,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
    runtime_account,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    RuntimeCredentialResolver,
)

ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=1)


def login_account(
    label: str = "claude-team",
    *,
    generation: str = "old",
    access_expiry: KnownExpiry | None = None,
) -> Account:
    """Build one synthetic refreshable Claude login."""
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeLoginCredentials(
            access_token=f"sk-ant-oat01-{generation}",
            refresh_token=f"refresh-{generation}",
            access_expiry=access_expiry or KnownExpiry(ACCESS_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
    )


def refresh_coordinator(
    store: AccountStore,
    provider: Provider,
    refresh_root: Path,
) -> CredentialRefreshCoordinator:
    """Compose one standard rotating-refresh transaction boundary."""
    return CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(store, refresh_root),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )


class BoundaryRecordingRefreshTransactions(CredentialRefreshTransactions):
    """Record setup-token crossings without creating persistence state."""

    def __init__(self, store: AccountStore, root: Path) -> None:
        super().__init__(store, root)
        self.crossings: list[str] = []

    @contextmanager
    def hold_lifecycle(self) -> Iterator[None]:
        """Record entry into lifecycle exclusion."""
        self.crossings.append("lifecycle")
        yield

    def recover(self) -> None:
        """Record a private refresh recovery scan."""
        self.crossings.append("recovery")


class RefreshProvider(Provider):
    """Return one scripted replacement without network access."""

    id = ProviderId.CLAUDE
    display_name = "Synthetic Claude"

    def __init__(self) -> None:
        self.calls: list[Account] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return DetectedCredentials(credentials=login_account().credentials)

    def credentials_from_token(self, token: str) -> CredentialDetection:
        del token
        return DetectedCredentials(credentials=login_account().credentials)

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        return UsageReport()

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        self.calls.append(account)
        previous = account.credentials
        assert isinstance(previous, ClaudeLoginCredentials)
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token="sk-ant-oat01-new",
                refresh_token="refresh-new",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=2)),
                refresh_expiry=UnknownExpiry(),
                scopes=previous.scopes,
                identity=previous.identity,
            ),
            plan="max",
        )


class CodexRefreshProvider(Provider):
    """Return one synthetic Codex replacement without provider I/O."""

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
            credentials=CodexCredentials(
                access_token="codex-access-new",
                refresh_token="codex-refresh-new",
                expiry=previous.expiry,
                account_id=previous.account_id,
                auth_home=previous.auth_home,
                id_token=previous.id_token,
                auth_last_refresh=previous.auth_last_refresh,
            )
        )


class BlockingRefreshProvider(RefreshProvider):
    """Block the first exchange while rejecting a duplicate exchange."""

    def __init__(self, entered: Event, release: Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release
        self._calls_lock = Lock()

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        with self._calls_lock:
            call_number = len(self.calls)
        if call_number:
            raise AssertionError("same credential exchanged more than once")
        self._entered.set()
        assert self._release.wait(timeout=5)
        return super()._refresh_credentials(account, http)


class ParallelRefreshProvider(RefreshProvider):
    """Require two distinct credential exchanges to overlap."""

    def __init__(self, barrier: Barrier) -> None:
        super().__init__()
        self._barrier = barrier
        self._calls_lock = Lock()

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        with self._calls_lock:
            self.calls.append(account)
        self._barrier.wait(timeout=5)
        previous = account.credentials
        assert isinstance(previous, ClaudeLoginCredentials)
        suffix = str(account.label)
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token=f"sk-ant-oat01-new-{suffix}",
                refresh_token=f"refresh-new-{suffix}",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=2)),
                refresh_expiry=UnknownExpiry(),
                scopes=previous.scopes,
            )
        )


class CallbackRefreshProvider(RefreshProvider):
    """Run one deterministic concurrent mutation during provider I/O."""

    def __init__(
        self,
        callback: Callable[[Account], RefreshResult],
    ) -> None:
        super().__init__()
        self._callback = callback

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        self.calls.append(account)
        return self._callback(account)


class ManagedStageRefreshProvider(RefreshProvider):
    """Require production refresh to use the transactions-owned stage."""

    def __init__(self) -> None:
        super().__init__()
        self.stage_home: Path | None = None

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del account, http
        raise AssertionError("raw rotating refresh bypassed managed stage")

    def refresh_credentials_in_stage(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        stage_home: Path,
        stage_reader: CredentialStageReader,
    ) -> RefreshResult:
        del stage_reader
        self.stage_home = stage_home
        return RefreshProvider._refresh_credentials(
            self,
            runtime_account(account),
            http,
        )


class BroadStageFailureProvider(ManagedStageRefreshProvider):
    """Create one conventional non-private directory before rejection."""

    def refresh_credentials_in_stage(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        stage_home: Path,
        stage_reader: CredentialStageReader,
    ) -> RefreshResult:
        del account, http, stage_reader
        self.stage_home = stage_home
        backups = stage_home / ".claude" / "backups"
        backups.mkdir(mode=0o755)
        backups.chmod(0o755)
        return ProviderFailure(
            provider_id=ProviderId.CLAUDE,
            kind=ProviderFailureKind.REJECTED,
            message="The provider rejected the refresh.",
        )


class SimulatedCrashError(Exception):
    """Stop one deterministic transaction at an exact durable point."""


@dataclass(frozen=True, slots=True)
class CrashAt:
    """Raise once when a transaction reaches the selected crash point."""

    point: CredentialRefreshCrashPoint

    def reached(self, point: CredentialRefreshCrashPoint) -> None:
        """Crash only at the selected transaction event."""
        if point is self.point:
            raise SimulatedCrashError
