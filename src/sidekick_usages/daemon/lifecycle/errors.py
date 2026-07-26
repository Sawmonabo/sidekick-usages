"""Typed failures for resident-service lifecycle operations."""

from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode
from sidekick_usages.errors import UsageError

_MESSAGES = {
    ServiceFailureCode.ARTIFACT_UNSAFE: (
        "The Sidekick user-service definition cannot be written safely."
    ),
    ServiceFailureCode.CANCELLED: (
        "The Sidekick user-service operation was cancelled."
    ),
    ServiceFailureCode.COMMAND_FAILED: (
        "The operating-system user-service command failed."
    ),
    ServiceFailureCode.EXECUTABLE_UNAVAILABLE: (
        "The installed Sidekick supervisor executable is unavailable."
    ),
    ServiceFailureCode.HANDSHAKE_FAILED: (
        "The Sidekick supervisor did not complete its local handshake."
    ),
    ServiceFailureCode.MAINTENANCE_TIMEOUT: (
        "The Sidekick supervisor did not finish its bounded readiness pass."
    ),
    ServiceFailureCode.QUEUE_INCOMPLETE: (
        "The Sidekick supervisor did not enroll every saved account."
    ),
    ServiceFailureCode.SERVICE_UNHEALTHY: (
        "The Sidekick user service did not reach a healthy running state."
    ),
    ServiceFailureCode.CODEX_BROKER_UNAVAILABLE: (
        "Managed Codex accounts require the Codex broker phase."
    ),
    ServiceFailureCode.PROVIDER_CAPABILITY_UNAVAILABLE: (
        "The required provider capability is unavailable."
    ),
}


class ServiceLifecycleError(UsageError):
    """One sanitized platform or readiness failure."""

    def __init__(self, code: ServiceFailureCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])
