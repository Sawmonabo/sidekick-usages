"""Closed shared Codex daemon types and ports."""

from enum import StrEnum


class CodexDaemonStatus(StrEnum):
    """Accepted official daemon lifecycle states."""

    STARTED = "started"
    ALREADY_RUNNING = "alreadyRunning"
    RESTARTED = "restarted"
    RUNNING = "running"


class CodexCallbackMode(StrEnum):
    """Closed private-worker projection operations."""

    REFRESH = "refresh"
    REHYDRATE = "rehydrate"


class CodexActivationMode(StrEnum):
    """Closed shared-runtime selection operations."""

    ACTIVATE = "activate"
    RECOVER = "recover"


class CodexBrokerFailure(StrEnum):
    """Secret-safe failures from the shared Codex runtime."""

    PLATFORM_UNSUPPORTED = "platform_unsupported"
    INSTALLATION_UNSUPPORTED = "installation_unsupported"
    VERSION_UNSUPPORTED = "version_unsupported"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    LIFECYCLE_FAILED = "lifecycle_failed"
    LIFECYCLE_MALFORMED = "lifecycle_malformed"
    DAEMON_UNMANAGED = "daemon_unmanaged"
    RUNTIME_UNSAFE = "runtime_unsafe"
    RUNTIME_CHANGED = "runtime_changed"
    CONNECTION_FAILED = "connection_failed"
    PROTOCOL_FAILED = "protocol_failed"
    PROJECTION_REJECTED = "projection_rejected"
    IDENTITY_MISMATCH = "identity_mismatch"
