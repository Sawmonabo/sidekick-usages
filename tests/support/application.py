"""Strict application-service test composition."""

from collections.abc import Mapping

from sidekick_usages.cli.contexts.models import AppContext
from sidekick_usages.clock import Clock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountLifecyclePersistence,
)
from sidekick_usages.credentials.accounts.lifecycle.service import (
    AccountLifecycleCoordinator,
)
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.codex.migration import (
    CodexAuthMigrationCoordinator,
)
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLocks,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude.types import (
    ClaudeSetupToken,
    SetupTokenCapture,
)
from sidekick_usages.usage.activity import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
)
from sidekick_usages.usage.lookup.service import AccountCredentialAccess
from sidekick_usages.usage.service import UsageCheckService
from tests.support.persistence import make_application_paths


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
    paths = make_application_paths(store.path.parent)
    resolver = credential_resolver_for(store, private_credentials)
    codex_profiles = PrivateCredentialTree(
        paths.private_codex_profiles,
        account_path=paths.accounts,
    )
    claude_profiles = PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
    refresh_coordinator = CredentialRefreshCoordinator(
        store,
        http,
        providers,
        CredentialRefreshTransactions(
            store,
            paths.credential_refresh,
        ),
        clock=clock,
        resolver=resolver,
    )
    credential_service = CredentialService(
        store,
        http,
        providers,
        refresh_coordinator=refresh_coordinator,
        codex_auth_migration=CodexAuthMigrationCoordinator(
            paths,
            store,
            codex_profiles,
            clock,
        ),
    )
    return AppContext(
        accounts=store,
        usage=UsageCheckService(
            store,
            http,
            providers,
            credential_service,
            clock=clock,
            credential_access=AccountCredentialAccess(
                resolver,
                OperationAuthorityLocks(paths.durable_operations),
            ),
            local_activity_sources=local_activity_sources,
            account_activity_sources=account_activity_sources,
        ),
        credentials=credential_service,
        lifecycle=AccountLifecycleCoordinator(
            paths,
            AccountLifecyclePersistence(
                accounts=store,
                operations=OperationQueueStore(paths.durable_operations),
                activations=ActivationJournalStore(
                    paths.activation_journals,
                    paths.durable_operations,
                ),
                selected=SelectedStateStore(paths.selected_state),
                claude_profiles=claude_profiles,
                codex_profiles=codex_profiles,
            ),
        ),
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
