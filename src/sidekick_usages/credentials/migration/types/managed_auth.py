"""Narrow capabilities used by managed-auth migration."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.migration.models.service import (
    ManagedAuthServiceResult,
)
from sidekick_usages.credentials.models import CredentialLoginResult

MANAGED_AUTH_PROVIDER_ORDER = (ProviderId.CODEX, ProviderId.CLAUDE)


class ManagedAuthAction(StrEnum):
    """One resumable action derived from the current account authority."""

    MIGRATE = "migrate"
    ASSOCIATE = "associate"
    VERIFY = "verify"


class ManagedAuthOutcome(StrEnum):
    """One account-scoped managed-auth migration outcome."""

    READY = "ready"
    ACTION_REQUIRED = "action_required"
    CANCELED = "canceled"


class ManagedAuthAccounts(Protocol):
    """Read current no-secret account authority state."""

    def saved_accounts(self) -> tuple[SavedAccount, ...]:
        """Return saved accounts in durable insertion order."""

    def read_saved(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | None:
        """Reopen one account by stable ID."""


class ManagedAuthServiceLifecycle(Protocol):
    """Ensure the existing per-user service is ready."""

    def status(self) -> ManagedAuthServiceResult:
        """Return current verified lifecycle state."""

    def install(self) -> ManagedAuthServiceResult:
        """Install and verify the user-level service."""

    def restart(self) -> ManagedAuthServiceResult:
        """Restart and verify the installed service."""


class CodexManagedMigration(Protocol):
    """Run the existing official Codex account migration."""

    def migrate(
        self,
        label: AccountLabel,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CredentialLoginResult:
        """Migrate or verify one final private Codex home."""


class ClaudeManagedMigration(Protocol):
    """Run the existing official Claude account migration."""

    def migrate(
        self,
        label: AccountLabel,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Migrate or verify one final private Claude profile."""

    def restore_setup_only(
        self,
        account_id: SidekickAccountId,
        *,
        expected_identity: ProviderIdentity,
    ) -> CredentialLoginResult:
        """Remove one explicitly rejected managed association."""
