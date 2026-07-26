"""Small ports shared by protected Claude authority owners."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    ClaudeManagedAuthorityResult,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
    ClaudeProtectedLogin,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner


class ClaudeAuthorityReader(Protocol):
    """Read one qualified native or private Claude authority."""

    def read(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity | None = None,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> ClaudeAuthoritySnapshot:
        """Return one strictly validated protected authority snapshot."""

    def open_login(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> AbstractContextManager[ClaudeProtectedLogin]:
        """Open one short-lived protected credential lease."""


class ClaudeAuthorityMaintainer(Protocol):
    """Maintain one current Claude authority under an existing lock."""

    def maintain_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> ClaudeManagedAuthorityResult:
        """Return one secret-free verified current account or outcome."""
