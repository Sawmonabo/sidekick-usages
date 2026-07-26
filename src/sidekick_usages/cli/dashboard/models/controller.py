"""Immutable interactive dashboard controller models."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId

type DashboardIntent = (
    ActivateOrRepairIntent | RefreshAccountIntent | RefreshDueAccountsIntent
)


class DashboardMove(StrEnum):
    """Closed cursor movement directions."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProviderAnchor:
    """One provider's verified-active or first-row restore target."""

    provider_id: ProviderId
    account_id: SidekickAccountId | None
    external: bool

    def __post_init__(self) -> None:
        """Require exactly one saved-account or external row reference."""
        if self.external == (self.account_id is not None):
            raise ValueError(
                "Dashboard anchor must identify one saved or external row."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardControllerState:
    """Secret-free interactive focus and restore state."""

    focused_provider: ProviderId | None
    account_id: SidekickAccountId | None
    external: bool
    anchors: tuple[DashboardProviderAnchor, ...]
    help_visible: bool = False

    def __post_init__(self) -> None:
        """Require one valid focused row and unique provider anchors."""
        if self.focused_provider is None:
            if self.account_id is not None or self.external:
                raise ValueError(
                    "An empty dashboard cannot identify a focused row."
                )
        elif self.external == (self.account_id is not None):
            raise ValueError(
                "Dashboard focus must identify one saved or external row."
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
class ActivateOrRepairIntent:
    """Request activation or repair of one saved account."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshAccountIntent:
    """Request refresh of one saved account without selecting it."""

    provider_id: ProviderId
    account_id: SidekickAccountId


@dataclass(frozen=True, slots=True)
class RefreshDueAccountsIntent:
    """Request refresh of every currently due saved account."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardActivationProof:
    """Verified successful activation returned by the service boundary."""

    provider_id: ProviderId
    account_id: SidekickAccountId
