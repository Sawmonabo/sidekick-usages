"""Domain types for optional usage-window heartbeat."""

from dataclasses import dataclass

HEARTBEAT_WARMED = "warmed"
HEARTBEAT_ACTIVE = "active"
HEARTBEAT_DISABLED = "disabled"
HEARTBEAT_UNSUPPORTED = "unsupported"
HEARTBEAT_FAILED = "failed"
HEARTBEAT_ENABLED = "enabled"


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
    reset_at: str | None = None
    message: str = "5h window inactive"


@dataclass(frozen=True)
class HeartbeatProbeResult:
    """Provider result after a tiny model request was sent."""

    status: str
    message: str
    warmed: bool
    reset_at: str | None = None
    action_required: bool = False
    target_id: str | None = None
    target_label: str | None = None


@dataclass(frozen=True)
class HeartbeatOutcome:
    """Service-level result for one optional heartbeat action."""

    label: str
    provider_id: str
    status: str
    message: str
    warmed: bool = False
    action_required: bool = False
    exit_code: int = 0
    target_id: str | None = None
    target_label: str | None = None
