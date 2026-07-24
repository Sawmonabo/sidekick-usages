"""Sanitized bounded resident diagnostic models."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.types.service import PackageVersion

MAX_DIAGNOSTIC_DURATION_MILLISECONDS = 24 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosticEvent:
    """One strictly sanitized resident diagnostic record."""

    observed_at: datetime
    phase: str
    result: str
    duration_milliseconds: int
    package_version: PackageVersion
    operation_id: OperationId | None = None
    account_id: SidekickAccountId | None = None
    provider_id: ProviderId | None = None

    def __post_init__(self) -> None:
        """Normalize time and validate safe categorical fields."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if safe_outcome_code(self.phase) is None:
            raise ValueError("Diagnostic phase must be a safe code.")
        if safe_outcome_code(self.result) is None:
            raise ValueError("Diagnostic result must be a safe code.")
        if (
            type(self.duration_milliseconds) is not int
            or not 0
            <= self.duration_milliseconds
            <= MAX_DIAGNOSTIC_DURATION_MILLISECONDS
        ):
            raise ValueError("Diagnostic duration is invalid.")
