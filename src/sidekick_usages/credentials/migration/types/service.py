"""Closed service-readiness states for managed-auth migration."""

from enum import StrEnum

MANAGED_AUTH_MESSAGE_MAX_BYTES = 1024


class ManagedAuthServiceState(StrEnum):
    """Provider-neutral next state for the resident service."""

    READY = "ready"
    INSTALL_REQUIRED = "install_required"
    RESTART_REQUIRED = "restart_required"
    BLOCKED = "blocked"
