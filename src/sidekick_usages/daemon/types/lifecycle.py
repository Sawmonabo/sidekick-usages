"""Closed values for per-user resident-service lifecycle."""

from enum import StrEnum

from sidekick_usages.core.types import ProviderId

type ProviderReadinessScope = tuple[ProviderId, ...]


class DaemonOperation(StrEnum):
    """Supported resident-service lifecycle operations."""

    INSTALL = "install"
    STATUS = "status"
    UNINSTALL = "uninstall"


class ServiceBackendId(StrEnum):
    """Supported operating-system service integrations."""

    SYSTEMD = "systemd"
    WSL = "wsl"
    LAUNCHD = "launchd"
    FEATURE_DISABLED = "feature-disabled"


class ServiceComponentState(StrEnum):
    """Independent health state for one supervisor component."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"
    FEATURE_DISABLED = "feature_disabled"


class ServiceLifecycleState(StrEnum):
    """Observed state of one complete platform integration."""

    ABSENT = "absent"
    INSTALLED = "installed"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    FEATURE_DISABLED = "feature_disabled"


class ServiceLifecyclePhase(StrEnum):
    """One transient phase emitted by its lifecycle operation owner."""

    INSTALLING = "installing"
    STARTING = "starting"
    CONTROL_SOCKET = "control_socket"
    DURABLE_RECOVERY = "durable_recovery"
    CODEX_BROKER = "codex_broker"
    PROVIDER_CAPABILITY = "provider_capability"
    MAINTENANCE_COMPLETED = "maintenance_completed"
    RESTARTING = "restarting"


class ServiceFailureCode(StrEnum):
    """Safe resident-service lifecycle failures."""

    ARTIFACT_UNSAFE = "artifact_unsafe"
    CANCELLED = "cancelled"
    COMMAND_FAILED = "command_failed"
    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    HANDSHAKE_FAILED = "handshake_failed"
    MAINTENANCE_TIMEOUT = "maintenance_timeout"
    QUEUE_INCOMPLETE = "queue_incomplete"
    SERVICE_UNHEALTHY = "service_unhealthy"
    CODEX_BROKER_UNAVAILABLE = "codex_broker_unavailable"
    PROVIDER_CAPABILITY_UNAVAILABLE = "provider_capability_unavailable"
