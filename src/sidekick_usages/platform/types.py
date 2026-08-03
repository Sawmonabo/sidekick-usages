"""Closed operating-system integration types."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.platform.models import PeerIdentity, ProcessIdentity

type WorkerEnvironment = tuple[tuple[str, str], ...]


class PeerFailureCode(StrEnum):
    """Safe reasons an operating system could not prove a local peer."""

    DIFFERENT_USER = "different_user"
    FEATURE_DISABLED = "feature_disabled"
    PROOF_UNAVAILABLE = "proof_unavailable"


class ProcessLiveness(StrEnum):
    """Exact process-start identity observation."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class ExecutableFailure(StrEnum):
    """Safe reasons exact executable qualification failed."""

    MISSING = "missing"
    UNSAFE = "unsafe"


class HostPlatform(StrEnum):
    """Closed host platforms relevant to Sidekick integrations."""

    LINUX = "linux"
    WSL = "wsl"
    MACOS_ARM64 = "macos_arm64"
    MACOS_X64 = "macos_x64"
    WINDOWS = "windows"
    UNSUPPORTED = "unsupported"


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


class ProcessIdentityInspector(Protocol):
    """Inspect exact process-start identity without sending a signal."""

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        """Return exact liveness or unknown when proof is unavailable."""


class ProcessGroup(Protocol):
    """One isolated subprocess group that can be reaped safely."""

    @property
    def process_id(self) -> int:
        """Return the native process identifier."""

    def poll(self) -> int | None:
        """Return its exit status when reaped."""

    def wait(self, timeout_seconds: float | None) -> int | None:
        """Wait up to a bound and return ``None`` on timeout."""

    def group_alive(self) -> bool:
        """Return whether any process remains in the isolated group."""

    def terminate_group(self) -> None:
        """Request termination of the isolated process group."""

    def kill_group(self) -> None:
        """Force termination of the isolated process group."""
