"""Secret-safe cross-provider managed-auth migration state."""

from dataclasses import dataclass, field

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.accounts.validation import require_bounded_text
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.migration.models.service import (
    ManagedAuthServiceResult,
)
from sidekick_usages.credentials.migration.types.managed_auth import (
    MANAGED_AUTH_PROVIDER_ORDER,
    ManagedAuthAction,
    ManagedAuthOutcome,
)
from sidekick_usages.credentials.migration.types.service import (
    MANAGED_AUTH_MESSAGE_MAX_BYTES,
    ManagedAuthServiceState,
)
from sidekick_usages.providers.base import ProviderFailureKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedAuthTarget:
    """One secret-safe migration target keyed by stable account ID."""

    provider_id: ProviderId
    account_id: SidekickAccountId = field(repr=False)
    label: AccountLabel
    action: ManagedAuthAction


@dataclass(frozen=True, slots=True)
class ManagedAuthPlan:
    """Deterministic Codex-then-Claude migration preview."""

    targets: tuple[ManagedAuthTarget, ...]

    def __post_init__(self) -> None:
        """Require unique accounts in canonical migration order."""
        account_ids = tuple(target.account_id for target in self.targets)
        order = tuple(
            MANAGED_AUTH_PROVIDER_ORDER.index(target.provider_id)
            for target in self.targets
        )
        if len(account_ids) != len(set(account_ids)) or order != tuple(
            sorted(order)
        ):
            raise ValueError("Managed-auth migration plan is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedAuthAccountResult:
    """One account-specific result without provider identity or credentials."""

    provider_id: ProviderId
    account_id: SidekickAccountId = field(repr=False)
    label: AccountLabel
    outcome: ManagedAuthOutcome
    message: str
    failure_kind: ProviderFailureKind | None = None

    def __post_init__(self) -> None:
        """Require bounded copy and explicit failure classification."""
        require_bounded_text(
            self.message,
            name="Migration result message",
            maximum=MANAGED_AUTH_MESSAGE_MAX_BYTES,
        )
        ready = self.outcome is ManagedAuthOutcome.READY
        if ready == (self.failure_kind is not None):
            raise ValueError("Migration outcome and failure kind disagree.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedAuthReport:
    """Complete service and account migration result."""

    service: ManagedAuthServiceResult
    accounts: tuple[ManagedAuthAccountResult, ...]
    all_accounts_verified: bool

    @property
    def complete(self) -> bool:
        """Return whether service and every account are proven ready."""
        return (
            self.service.state is ManagedAuthServiceState.READY
            and self.all_accounts_verified
            and all(
                account.outcome is ManagedAuthOutcome.READY
                for account in self.accounts
            )
        )
