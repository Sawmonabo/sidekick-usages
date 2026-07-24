"""Closed shared Codex daemon types and ports."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)


class CodexDaemonStatus(StrEnum):
    """Accepted official daemon lifecycle states."""

    STARTED = "started"
    ALREADY_RUNNING = "alreadyRunning"
    RUNNING = "running"


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


class CodexProjection(Protocol):
    """Expose one short-lived locally proven account projection."""

    @property
    def account_id(self) -> SidekickAccountId:
        """Return the stable Sidekick account identifier."""

    @property
    def provider_identity(self) -> ProviderIdentity:
        """Return the locally proven ChatGPT account identifier."""

    @property
    def generation(self) -> AuthorityGeneration:
        """Return the protected managed-home generation."""

    @property
    def plan(self) -> str:
        """Return the validated plan supplied by managed Codex."""

    @property
    def access_token(self) -> str:
        """Return the credential only while the projection is active."""
