"""Domain types for optional usage-window heartbeat."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
)


@dataclass(frozen=True)
class HeartbeatTarget:
    """A provider-specific usage window that can be warmed."""

    id: str
    label: str
    default: bool = False


@dataclass(frozen=True)
class UsageWindowState:
    """Provider-neutral state for the window heartbeat cares about."""

    active: bool
    reset_at: datetime | None = None
    message: str = "5h window inactive"

    def __post_init__(self) -> None:
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", as_utc(self.reset_at))


@dataclass(frozen=True)
class HeartbeatProbeResult:
    """Provider result after a tiny model request was sent."""

    status: HeartbeatStatus
    message: str
    warmed: bool
    reset_at: datetime | None = None
    action_required: bool = False
    target_id: str | None = None
    target_label: str | None = None

    def __post_init__(self) -> None:
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", as_utc(self.reset_at))


@dataclass(frozen=True)
class HeartbeatOutcome:
    """Service-level result for one optional heartbeat action."""

    label: AccountLabel | None
    provider_id: ProviderId | None
    status: HeartbeatStatus
    message: str
    warmed: bool = False
    action_required: bool = False
    exit_code: ExitCode = ExitCode.SUCCESS
    target_id: str | None = None
    target_label: str | None = None
