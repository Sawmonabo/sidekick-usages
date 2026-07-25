"""Ports for projecting managed Codex authorities into native runtime."""

from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection


class CodexProjectionInstaller(Protocol):
    """Preflight and install one ephemeral shared-runtime authority."""

    def prepare(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
    ) -> CodexProjectionReceipt | None:
        """Return a matching live receipt or request a fresh projection."""

    def install(
        self,
        projection: CodexProjection,
    ) -> CodexProjectionReceipt:
        """Install one active projection after successful preflight."""
