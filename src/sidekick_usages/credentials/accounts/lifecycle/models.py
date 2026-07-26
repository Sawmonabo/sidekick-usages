"""Secret-safe saved-account lifecycle models."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.accounts.removal.models import (
    AccountRemovalRecord,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

type AccountRemovalResult = (
    AccountRemovalSuccess
    | AccountRemovalFailure
    | AccountRemovalPartialFailure
    | AccountProfileFailure
)


class AccountRemovalFailureKind(StrEnum):
    """Closed reasons an account removal did not fully complete."""

    MISSING = "missing"
    SELECTED = "selected"
    ACTIVE_ACTIVATION = "active_activation"
    RUNNING_OPERATION = "running_operation"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROFILE_UNSAFE = "profile_unsafe"
    PROFILE_UNMAPPABLE = "profile_unmappable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    STATE_CHANGED = "state_changed"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
    PROFILE_CLEANUP_FAILED = "profile_cleanup_failed"


@dataclass(frozen=True, slots=True)
class AccountLifecyclePersistence:
    """Persistence boundaries required for guarded account removal."""

    accounts: AccountStore
    operations: OperationQueueStore
    activations: ActivationJournalStore
    selected: SelectedStateStore
    claude_profiles: PrivateCredentialTree
    codex_profiles: PrivateCredentialTree


@dataclass(frozen=True, slots=True)
class AccountLifecycleRuntime:
    """Injectable host and process boundaries for Claude retirement."""

    environment: Mapping[str, str] | None = None
    host: HostPlatform | None = None
    runner: ClaudeCommandRunner = run_bounded_claude_command


@dataclass(frozen=True, slots=True)
class AccountRemovalSuccess:
    """One saved account and its private provider state were removed."""

    account_id: SidekickAccountId
    label: AccountLabel | None
    provider_id: ProviderId

    @classmethod
    def from_account(cls, account: SavedAccount) -> AccountRemovalSuccess:
        """Project one removed account without provider identity metadata."""
        return cls(
            account.account_id,
            account.label,
            account.provider_id,
        )

    @classmethod
    def from_record(
        cls,
        record: AccountRemovalRecord,
    ) -> AccountRemovalSuccess:
        """Project recovery success without persisting an account label."""
        return cls(
            record.account_id,
            None,
            record.provider_id,
        )


@dataclass(frozen=True, slots=True)
class AccountRemovalFailure:
    """One account removal failed before account metadata was removed."""

    account_id: SidekickAccountId
    kind: AccountRemovalFailureKind
    message: str
    action_required: bool


@dataclass(frozen=True, slots=True)
class AccountRemovalPartialFailure:
    """Account metadata was removed but private cleanup did not finish."""

    account_id: SidekickAccountId
    label: AccountLabel | None
    provider_id: ProviderId
    kind: AccountRemovalFailureKind
    message: str
    action_required: bool

    def __post_init__(self) -> None:
        """Require a failure valid after saved metadata removal."""
        if self.kind not in {
            AccountRemovalFailureKind.SELECTED,
            AccountRemovalFailureKind.ACTIVE_ACTIVATION,
            AccountRemovalFailureKind.RUNNING_OPERATION,
            AccountRemovalFailureKind.PROVIDER_UNAVAILABLE,
            AccountRemovalFailureKind.PROFILE_UNSAFE,
            AccountRemovalFailureKind.RECONCILIATION_REQUIRED,
            AccountRemovalFailureKind.STATE_CHANGED,
            AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
            AccountRemovalFailureKind.PROFILE_CLEANUP_FAILED,
        }:
            raise ValueError("Partial removal failure kind is invalid.")


@dataclass(frozen=True, slots=True)
class AccountProfileFailure:
    """One unmappable or unsafe provider profile blocked reset."""

    provider_id: ProviderId
    profile_basename: str
    kind: AccountRemovalFailureKind
    message: str
    action_required: bool

    def __post_init__(self) -> None:
        """Require a profile-specific fail-closed outcome."""
        if self.kind not in {
            AccountRemovalFailureKind.PROFILE_UNMAPPABLE,
            AccountRemovalFailureKind.PROFILE_UNSAFE,
        }:
            raise ValueError("Profile failure kind is invalid.")
