"""Structural ports private to isolated worker processes."""

from typing import Protocol

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.usage.models import UsageCheckResult


class AccountMetricsCollector(Protocol):
    """Collect exact-account metrics under existing worker authority."""

    def collect(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> UsageCheckResult:
        """Return current or explicitly stale metrics for one account."""
