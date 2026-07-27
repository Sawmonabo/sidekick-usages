"""Provider-owned production composition for managed Codex authority."""

from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexProcessGroupPolicy,
)


def compose_codex_managed_authority(
    paths: ApplicationPaths,
    store: AccountStore,
    profiles: PrivateCredentialTree,
    clock: Clock,
    environment: Mapping[str, str],
    *,
    launcher: Path | None,
) -> CodexManagedAuthorityCoordinator:
    """Build one capability-proven managed Codex authority coordinator."""
    capabilities = probe_codex_capabilities(
        discover_codex_executable(
            environment,
            launcher=launcher,
            process_group=CodexProcessGroupPolicy.INHERITED,
        ),
        environment,
        process_group=CodexProcessGroupPolicy.INHERITED,
    )
    return CodexManagedAuthorityCoordinator(
        paths,
        store,
        profiles,
        capabilities,
        clock,
        environment=environment,
    )
