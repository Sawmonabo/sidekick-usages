"""Provider-managed credential resolver composition."""

from collections.abc import Mapping

from sidekick_usages.clock import Clock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import CredentialLeaseFactory
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationRuntime,
)
from sidekick_usages.credentials.claude.authority.resolver import (
    ClaudeManagedCredentialResolver,
)
from sidekick_usages.credentials.claude.managed.maintenance.service import (
    ClaudeManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.credentials.codex.managed.composition import (
    compose_codex_managed_authority,
)
from sidekick_usages.credentials.codex.managed.resolver import (
    CodexManagedCredentialResolver,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)


def compose_managed_credential_factories(
    paths: ApplicationPaths,
    persistence: PersistenceService,
    store: AccountStore,
    clock: Clock,
    environment: Mapping[str, str],
    *,
    claude_runtime: ClaudeActivationRuntime | None = None,
) -> dict[ProviderId, CredentialLeaseFactory]:
    """Compose available provider-owned managed credential resolvers."""
    accounts = store.saved_accounts()
    factories: dict[ProviderId, CredentialLeaseFactory] = {}
    managed_claude = any(
        account.provider_id is ProviderId.CLAUDE
        and account.has_managed_authority
        for account in accounts
    )
    managed_codex = any(
        account.provider_id is ProviderId.CODEX
        and account.has_managed_authority
        for account in accounts
    )
    if managed_claude:
        runtime = (
            ClaudeActivationRuntime(environment=environment)
            if claude_runtime is None
            else claude_runtime
        )
        profiles = persistence.managed_claude_profiles
        try:
            capabilities = ClaudeProfileCapabilityFactory(
                paths,
                profiles,
                environment=runtime.environment,
                host=runtime.host,
                runner=runtime.runner,
            )
        except ClaudeManagedError:
            pass
        else:
            selected = SelectedStateStore(paths.selected_state)
            activation = ClaudeActivationAuthorityCoordinator(
                paths,
                store,
                profiles,
                clock,
                capabilities=capabilities,
                runtime=runtime,
            )
            maintainer = ClaudeManagedAuthorityCoordinator(
                paths,
                store,
                profiles,
                selected,
                activation,
                capabilities,
                clock,
                environment=runtime.environment,
                runner=runtime.runner,
            )
            resolver = ClaudeManagedCredentialResolver(
                paths,
                profiles,
                selected,
                maintainer,
                capabilities,
                clock,
                environment=runtime.environment,
                runner=runtime.runner,
            )
            factories[ProviderId.CLAUDE] = resolver.open_authorized
    if not managed_codex:
        return factories
    try:
        coordinator = compose_codex_managed_authority(
            paths,
            store,
            persistence.managed_codex_profiles,
            clock,
            environment,
            executable_path=None,
        )
    except CodexAppServerError:
        return factories
    resolver = CodexManagedCredentialResolver(coordinator)
    factories[ProviderId.CODEX] = resolver.open_authorized
    return factories
