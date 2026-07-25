"""Structural ports private to isolated worker processes."""

from typing import Protocol

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.heartbeat.models import HeartbeatOutcome
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.usage.models import UsageCheckResult


class ManagedAccountService(Protocol):
    """Run exact-account services under an existing worker authority."""

    def collect_metrics(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> UsageCheckResult:
        """Return current or explicitly stale metrics for one account."""

    def heartbeat(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> tuple[HeartbeatOutcome, ...]:
        """Heartbeat enabled targets for one account."""
