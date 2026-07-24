"""Immutable shared Codex daemon authorities and receipts."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.types import CodexDaemonStatus


@dataclass(frozen=True, slots=True)
class CodexFilesystemIdentity:
    """Exact owner, mode, and inode for one local daemon object."""

    device: int
    inode: int
    owner_user_id: int
    mode: int

    def __post_init__(self) -> None:
        """Reject invalid native filesystem identity values."""
        if min(
            self.device,
            self.inode,
            self.owner_user_id,
            self.mode,
        ) < 0:
            raise ValueError("Codex daemon filesystem identity is invalid.")


@dataclass(frozen=True, slots=True)
class CodexDaemonLifecycle:
    """Strictly decoded official daemon lifecycle result."""

    status: CodexDaemonStatus
    managed_executable: Path
    socket_path: Path

    def __post_init__(self) -> None:
        """Require absolute managed-executable and socket paths."""
        if (
            not self.managed_executable.is_absolute()
            or not self.socket_path.is_absolute()
        ):
            raise ValueError("Codex daemon lifecycle result is invalid.")


@dataclass(frozen=True, slots=True)
class CodexDaemonAuthority:
    """One qualified official daemon and its exact socket objects."""

    lifecycle: CodexDaemonLifecycle
    executable: CodexExecutable
    control_directory: CodexFilesystemIdentity
    control_socket: CodexFilesystemIdentity


@dataclass(frozen=True, slots=True)
class CodexProjectionExpectation:
    """Non-secret selected authority expected by one install operation."""

    account_id: SidekickAccountId
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration


@dataclass(frozen=True, slots=True)
class CodexProjectionReceipt:
    """Correlated-ready proof without an independent daemon identity read."""

    account_id: SidekickAccountId
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    plan: str
    socket_device: int
    socket_inode: int

    def __post_init__(self) -> None:
        """Validate bounded metadata and exact local runtime identity."""
        require_bounded_text(
            self.plan,
            name="Codex projection plan",
            maximum=MAX_METADATA_BYTES,
        )
        if min(
            self.socket_device,
            self.socket_inode,
        ) < 1:
            raise ValueError("Codex projection runtime identity is invalid.")
