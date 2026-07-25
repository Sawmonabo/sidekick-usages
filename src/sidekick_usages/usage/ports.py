"""Persistence ports for account-scoped usage observations."""

from dataclasses import dataclass
from typing import Protocol

from sidekick_usages.core.accounts.models import SavedAccount
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


class AccountUsageSnapshots(Protocol):
    """Persist last-successful usage by stable account identity."""

    def load(self, account: SavedAccount) -> AccountUsageSnapshot | None:
        """Load the account's last successful authenticated usage."""

    def save(
        self,
        snapshot: AccountUsageSnapshot,
    ) -> AccountUsageSnapshot:
        """Durably merge one successful authenticated usage."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UsagePersistence:
    """Optional durable observations used by one usage service."""

    activity: AccountTokenActivitySnapshots | None = None
    usage: AccountUsageSnapshots | None = None
