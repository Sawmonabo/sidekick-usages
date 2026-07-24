"""Closed operating-system integration types."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.platform.models import PeerIdentity


class PeerFailureCode(StrEnum):
    """Safe reasons an operating system could not prove a local peer."""

    DIFFERENT_USER = "different_user"
    FEATURE_DISABLED = "feature_disabled"
    PROOF_UNAVAILABLE = "proof_unavailable"


class PeerSocket(Protocol):
    """Socket operations required for operating-system peer proof."""

    def fileno(self) -> int:
        """Return the live file descriptor."""

    def getsockopt(
        self,
        level: int,
        option: int,
        buffer_length: int,
        /,
    ) -> bytes:
        """Read one socket option."""


class PeerVerifier(Protocol):
    """Prove that a local connection belongs to the effective user."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Return a verified identity or fail closed."""
