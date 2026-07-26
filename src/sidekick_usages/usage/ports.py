"""Persistence ports for account-scoped usage observations."""

from dataclasses import dataclass
from typing import Protocol

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    AccountUsageSnapshot,
)


class AccountTokenActivitySnapshots(Protocol):
    """Persist authoritative activity by stable account identity."""

    def load(
        self,
        account: SavedAccount,
    ) -> AccountTokenActivitySnapshot | None:
        """Load the account's last successful activity snapshot."""

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        """Durably merge one successful account activity snapshot."""

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountTokenActivitySnapshot]:
        """Load exact account snapshots through one document decode."""

    def save_many(
        self,
        snapshots: tuple[AccountTokenActivitySnapshot, ...],
    ) -> tuple[AccountTokenActivitySnapshot, ...]:
        """Durably merge observations through one document commit."""


class AccountUsageSnapshots(Protocol):
    """Persist last-successful usage by stable account identity."""

    def load(self, account: SavedAccount) -> AccountUsageSnapshot | None:
        """Load the account's last successful authenticated usage."""

    def save(
        self,
        snapshot: AccountUsageSnapshot,
    ) -> AccountUsageSnapshot:
        """Durably merge one successful authenticated usage."""

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountUsageSnapshot]:
        """Load exact account snapshots through one document decode."""

    def save_many(
        self,
        snapshots: tuple[AccountUsageSnapshot, ...],
    ) -> tuple[AccountUsageSnapshot, ...]:
        """Durably merge observations through one document commit."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UsagePersistence:
    """Optional durable observations used by one usage service."""

    activity: AccountTokenActivitySnapshots | None = None
    usage: AccountUsageSnapshots | None = None
