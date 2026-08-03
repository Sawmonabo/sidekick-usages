"""Immutable interactive dashboard controller models."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.core.types import ProviderId

type DashboardIntent = (
    SelectAccountIntent | RefreshAccountIntent | RefreshDueAccountsIntent
)
type DashboardApplicationResult = int


class DashboardMove(StrEnum):
    """Closed cursor movement directions."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProviderAnchor:
    """One provider's saved-account restore target."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardControllerState:
    """Secret-free interactive focus and restore state."""

    focused_provider: ProviderId | None
    account_id: SidekickAccountId | None
    anchors: tuple[DashboardProviderAnchor, ...]
    help_visible: bool = False

    def __post_init__(self) -> None:
        """Require valid provider focus and unique provider anchors."""
        if self.focused_provider is None and self.account_id is not None:
            raise ValueError(
                "An empty dashboard cannot identify a focused row."
            )
        provider_ids = tuple(anchor.provider_id for anchor in self.anchors)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Dashboard anchors must use unique providers.")
        if (
            self.focused_provider is not None
            and self.focused_provider not in provider_ids
        ):
            raise ValueError(
                "Dashboard focus must belong to a provider anchor."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectAccountIntent:
    """Request coordinated selection of one saved account."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardSelectionRefusal:
    """Explain why a focused saved account cannot be selected."""

    provider_id: ProviderId
    account_id: SidekickAccountId
    code: SelectionCode


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshAccountIntent:
    """Request refresh of one saved account without selecting it."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True)
class RefreshDueAccountsIntent:
    """Request refresh of every currently due saved account."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardSelectionProof:
    """Verified successful selection returned by the service boundary."""

    provider_id: ProviderId
    account_id: SidekickAccountId
