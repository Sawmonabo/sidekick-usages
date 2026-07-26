"""Persistence ports for account-scoped usage observations."""

from dataclasses import dataclass
from typing import Protocol

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    AccountUsageSnapshot,
    ProviderTokenActivitySnapshot,
)


class TokenActivitySnapshots(Protocol):
    """Persist account and provider activity in one document."""

    def load_many(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> dict[SidekickAccountId, AccountTokenActivitySnapshot]:
        """Load exact account snapshots through one document decode."""

    def save_many(
        self,
        accounts: tuple[AccountTokenActivitySnapshot, ...],
        providers: tuple[ProviderTokenActivitySnapshot, ...],
    ) -> tuple[
        tuple[AccountTokenActivitySnapshot, ...],
        tuple[ProviderTokenActivitySnapshot, ...],
    ]:
        """Durably merge one activity batch through one document commit."""


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

    activity: TokenActivitySnapshots | None = None
    usage: AccountUsageSnapshots | None = None
