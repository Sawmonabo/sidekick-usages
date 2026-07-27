"""Concurrency capabilities required by account lookup."""

from contextlib import AbstractContextManager
from typing import Protocol

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)


class AccountOperationLocks(Protocol):
    """Serialize provider work by stable saved-account identity."""

    def hold(
        self,
        account_id: SidekickAccountId,
    ) -> AbstractContextManager[OperationAuthority]:
        """Hold one account operation lock for the complete lookup."""
