"""Shared deterministic test dependencies."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never
from uuid import UUID, uuid5

from rich.console import Console
from typer.testing import CliRunner, Result

from sidekick_usages.cli import app
from sidekick_usages.cli.context import (
    AppContext,
    Composed,
    DaemonContext,
    DoctorContext,
    InvocationContext,
    PersistenceContext,
    UpdateContext,
)
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials import CredentialService
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialLease,
    credential_resolver_for,
)
from sidekick_usages.credentials.codex import CodexCredentialCoordinator
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
)
from sidekick_usages.daemon.types.service import PackageVersion
from sidekick_usages.heartbeat import HeartbeatProvider, HeartbeatService
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_index import (
    saved_account_from_runtime,
)
from sidekick_usages.persistence.account_runtime_bridge import (
    active_stored_reference,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.credential_repository import (
    authority_for_account,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.base import (
    CredentialAccountLease,
    Provider,
    ProviderAuthenticatedAccount,
)
from sidekick_usages.providers.claude import (
    ClaudeSetupToken,
    SetupTokenCapture,
)
from sidekick_usages.usage import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
    UsageCheckService,
)

REFERENCE_TIME = datetime(2026, 6, 12, 12, 34, 56, 789000, tzinfo=UTC)
_TEST_ACCOUNT_NAMESPACE = UUID("75cc2b04-05ea-43d2-b897-bc960c85cd63")
_TEST_AUTHORITY_NAMESPACE = UUID("a050a4a2-357b-4923-aeed-ed5866475853")


@dataclass(frozen=True, slots=True)
class _TestCredentialLease:
    """Expose a synthetic account at the provider test boundary."""

    account: Account


def make_supervisor_health(
    *,
    queue: ServiceComponentState = ServiceComponentState.HEALTHY,
) -> SupervisorHealth:
    """Return one synthetic current supervisor observation."""
    return SupervisorHealth(
        backend=ServiceBackendId.SYSTEMD,
        cli_version=PackageVersion("0.7.0"),
        supervisor_version=PackageVersion("0.7.0"),
        platform=ServiceComponentState.HEALTHY,
        process=ServiceComponentState.HEALTHY,
        protocol=ServiceComponentState.HEALTHY,
        queue=queue,
        journal=ServiceComponentState.HEALTHY,
        broker=ServiceComponentState.NOT_REQUIRED,
    )


def saved_account(account: Account) -> SavedAccount:
    """Return secret-free metadata for one synthetic runtime account."""
    identity = f"{account.provider_id.value}\0{account.label}"
    return saved_account_from_runtime(
        account,
        account_id=SidekickAccountId(
            str(uuid5(_TEST_ACCOUNT_NAMESPACE, identity))
        ),
        authority_id=AuthorityId(
            str(uuid5(_TEST_AUTHORITY_NAMESPACE, identity))
        ),
    )


def authenticated_account(account: Account) -> ProviderAuthenticatedAccount:
    """Wrap one synthetic runtime account for a direct provider test."""
    lease: CredentialAccountLease = _TestCredentialLease(account)
    return AuthenticatedAccount(account=saved_account(account), lease=lease)


class RuntimeCredentialResolver:
    """Open typed test leases from one in-memory runtime account source."""

    def __init__(self, source: AccountStore) -> None:
        self._source = source
        self.events: list[str] = []

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return one unopened synthetic credential lease."""
        return self._open(account)

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
    ) -> Iterator[AuthenticatedSavedAccount]:
        self.events.append(f"open:{account.label}")
        runtime = self._source.get(
            str(account.label),
            provider_id=account.provider_id,
        )
        if runtime is None:
            raise AssertionError("Resolver target disappeared.")
        authority = authority_for_account(
            runtime,
            account_id=account.account_id,
            authority_id=active_stored_reference(account),
        )
        lease = CredentialLease(account, authority)
        with lease:
            yield AuthenticatedAccount(account=account, lease=lease)
        self.events.append(f"close:{account.label}")


def make_application_paths(root: Path) -> ApplicationPaths:
    """Build isolated Sidekick-owned locations below ``root``."""
    account_file = root / "accounts.json"
    private_root = root / "credentials"
    return ApplicationPaths(
        accounts=account_file,
        private_credentials=private_root,
        private_codex_profiles=private_root / "codex",
        activity_snapshots=root / "token-activity.json",
        credential_refresh=root / "credential-refresh",
        private_claude_profiles=private_root / "claude",
        selected_state=root / "selected-accounts.json",
        activation_journals=root / "activation-journals",
        durable_operations=root / "operations",
        service_state=root / "service-state.json",
        service_logs=root / "logs",
        runtime_directory=root / "runtime",
        supervisor_socket=root / "runtime" / "supervisor.sock",
        systemd_user_service=root / "home/systemd/sidekick-usages.service",
        launch_agent=(
            root
            / "home"
            / "LaunchAgents"
            / "com.sidekick-usages.supervisor.plist"
        ),
    )


def make_account_store(
    root: Path,
    accounts: Iterable[Account] = (),
) -> AccountStore:
    """Build a loaded transactional store with a live private observer."""
    store, _private = make_account_store_with_private(root, accounts)
    return store


def make_account_store_with_private(
    root: Path,
    accounts: Iterable[Account] = (),
) -> tuple[AccountStore, PrivateCredentialTree]:
    """Build a store and the exact private tree injected into it."""
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts).repair_parent_permissions()
    private_credentials = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    store = AccountStore(paths.accounts, private_credentials).load()
    for account in accounts:
        store.persist(account)
    return store, private_credentials


@dataclass(slots=True)
class FixedClock:
    """Return one fixed instant while counting wall-time acquisitions."""

    value: datetime = REFERENCE_TIME
    calls: int = 0

    def now(self) -> datetime:
        """Return the fixed instant and record one acquisition."""
        self.calls += 1
        return self.value


class _UnexpectedClaudeSetupToken:
    """Fail if a command crosses an unconfigured setup-token boundary."""

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        del timeout
        raise AssertionError("Claude setup-token composition was unexpected.")


def make_app_context(
    store: AccountStore,
    http: HttpClient,
    providers: dict[ProviderId, Provider],
    private_credentials: PrivateCredentialTree,
    clock: Clock,
    *,
    heartbeat_providers: Mapping[ProviderId, HeartbeatProvider] | None = None,
    local_activity_sources: Mapping[
        ProviderId,
        LocalTokenActivitySource,
    ]
    | None = None,
    account_activity_sources: Mapping[
        ProviderId,
        AccountTokenActivitySource,
    ]
    | None = None,
    claude_setup_token: ClaudeSetupToken | None = None,
) -> AppContext:
    """Build strict application services around test-owned boundaries."""
    heartbeat_map = (
        {} if heartbeat_providers is None else dict(heartbeat_providers)
    )
    resolver = credential_resolver_for(store, private_credentials)
    codex_coordinator = CodexCredentialCoordinator(
        store,
        private_credentials,
        clock=clock,
    )
    refresh_coordinator = CredentialRefreshCoordinator(
        store,
        http,
        providers,
        CredentialRefreshTransactions(
            store,
            make_application_paths(store.path.parent).credential_refresh,
        ),
        clock=clock,
        codex=codex_coordinator,
        resolver=resolver,
    )
    credential_service = CredentialService(
        store,
        http,
        providers,
        private_credentials,
        clock=clock,
        refresh_coordinator=refresh_coordinator,
        codex_coordinator=codex_coordinator,
    )
    return AppContext(
        accounts=store,
        usage=UsageCheckService(
            store,
            http,
            providers,
            credential_service,
            clock=clock,
            local_activity_sources=local_activity_sources,
            account_activity_sources=account_activity_sources,
            resolver=resolver,
        ),
        credentials=credential_service,
        heartbeat=HeartbeatService(
            store,
            http,
            heartbeat_map,
            clock=clock,
            resolver=resolver,
        ),
        maintenance=TokenMaintenanceService(
            store,
            credential_service,
            clock=clock,
        ),
        claude_setup_token=(
            _UnexpectedClaudeSetupToken()
            if claude_setup_token is None
            else claude_setup_token
        ),
    )


def _unexpected_composition() -> Never:
    raise AssertionError("Command crossed an unconfigured composition path.")


def _fixed_composer[T](value: T) -> Callable[[], Composed[T]]:
    def compose() -> Composed[T]:
        return Composed(value, ExitStack())

    return compose


@dataclass(slots=True)
class CliHarness:
    """Invoke the CLI with fresh, explicitly configured typed composers."""

    console: Console
    err_console: Console
    application: AppContext | None = None
    persistence: PersistenceContext | None = None
    doctor: DoctorContext | None = None
    daemon: DaemonContext | None = None
    update: UpdateContext | None = None

    def invoke(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> Result:
        """Invoke with new presentation and composition state."""
        return CliRunner().invoke(
            app,
            arguments,
            input=input_text,
            obj=InvocationContext(
                console=self.console,
                err_console=self.err_console,
                app_composer=(
                    _unexpected_composition
                    if self.application is None
                    else _fixed_composer(self.application)
                ),
                persistence_composer=(
                    _unexpected_composition
                    if self.persistence is None
                    else _fixed_composer(self.persistence)
                ),
                doctor_composer=(
                    _unexpected_composition
                    if self.doctor is None
                    else _fixed_composer(self.doctor)
                ),
                daemon_composer=(
                    _unexpected_composition
                    if self.daemon is None
                    else _fixed_composer(self.daemon)
                ),
                update_composer=(
                    _unexpected_composition
                    if self.update is None
                    else _fixed_composer(self.update)
                ),
            ),
        )


__all__ = [
    "REFERENCE_TIME",
    "CliHarness",
    "FixedClock",
    "RuntimeCredentialResolver",
    "make_account_store",
    "make_account_store_with_private",
    "make_app_context",
    "make_application_paths",
]
