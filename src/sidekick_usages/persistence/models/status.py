"""Secret-free current persistence status models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.models.credential import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.persistence.types.status import PersistenceState


@dataclass(frozen=True, slots=True)
class PersistenceStatus:
    """Validated status of the sole supported account store."""

    state: PersistenceState
    path: Path
    account_count: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("Persistence status path must be absolute.")
        if self.account_count < 0:
            raise ValueError("Account count cannot be negative.")


@dataclass(frozen=True, slots=True)
class PersistenceFailure:
    """Bounded failure from current persistence composition."""

    code: PersistenceCode
    path: Path
    message: str
    artifact_basename: str | None = None

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("Persistence failure path must be absolute.")
        if not self.message:
            raise ValueError("Persistence failure message cannot be empty.")


@dataclass(frozen=True, slots=True)
class PermissionRepairResult:
    """Verified permission repair plus fresh store status."""

    repair: PrivateCredentialRepairResult
    status: PersistenceStatus
