"""Immutable shared Codex daemon authorities and receipts."""

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.types import (
    CodexCallbackMode,
    CodexDaemonStatus,
)


@dataclass(frozen=True, slots=True)
class CodexFilesystemIdentity:
    """Exact owner, mode, and inode for one local daemon object."""

    device: int
    inode: int
    owner_user_id: int
    mode: int

    def __post_init__(self) -> None:
        """Reject invalid native filesystem identity values."""
        if (
            min(
                self.device,
                self.inode,
                self.owner_user_id,
                self.mode,
            )
            < 0
        ):
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
        if (
            min(
                self.socket_device,
                self.socket_inode,
            )
            < 1
        ):
            raise ValueError("Codex projection runtime identity is invalid.")


@dataclass(frozen=True, slots=True)
class CodexRefreshRequest:
    """One strict external-token refresh request from the shared daemon."""

    request_id: int
    previous_provider_identity: ProviderIdentity


@dataclass(frozen=True, slots=True)
class CodexCallbackInstruction:
    """Non-secret correlation sent to one isolated Codex worker."""

    operation_id: OperationId
    mode: CodexCallbackMode
    account_id: SidekickAccountId
    provider_identity: ProviderIdentity
    source_generation: AuthorityGeneration
    response_deadline_nanoseconds: int
    completion_deadline_nanoseconds: int

    def __post_init__(self) -> None:
        """Require ordered same-machine monotonic deadlines."""
        if (
            type(self.response_deadline_nanoseconds) is not int
            or type(self.completion_deadline_nanoseconds) is not int
            or self.response_deadline_nanoseconds < 1
            or self.completion_deadline_nanoseconds
            <= self.response_deadline_nanoseconds
        ):
            raise ValueError("Codex callback deadlines are invalid.")

    @property
    def response_deadline_seconds(self) -> float:
        """Return the provider-response deadline in seconds."""
        return self.response_deadline_nanoseconds / 1_000_000_000

    @property
    def completion_deadline_seconds(self) -> float:
        """Return the post-response completion deadline in seconds."""
        return self.completion_deadline_nanoseconds / 1_000_000_000


@dataclass(frozen=True, slots=True)
class CodexCallbackAcknowledgement:
    """Secret-free proof that the supervisor dispatched one response."""

    operation_id: OperationId
    mode: CodexCallbackMode
    generation: AuthorityGeneration


class CodexRefreshReplyLease:
    """Short-lived decoded worker response with redacted credential state."""

    __slots__ = (
        "_access_token",
        "_active",
        "account_id",
        "generation",
        "mode",
        "operation_id",
        "plan",
        "provider_identity",
        "source_generation",
    )

    def __init__(
        self,
        operation_id: OperationId,
        mode: CodexCallbackMode,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        source_generation: AuthorityGeneration,
        generation: AuthorityGeneration,
        plan: str,
        access_token: str,
    ) -> None:
        self.operation_id = operation_id
        self.mode = mode
        self.account_id = account_id
        self.provider_identity = provider_identity
        self.source_generation = source_generation
        self.generation = generation
        self.plan = require_bounded_text(
            plan,
            name="Codex callback plan",
            maximum=MAX_METADATA_BYTES,
        )
        self._access_token: str | None = access_token
        self._active = False

    @property
    def access_token(self) -> str:
        """Return the credential only while the reply lease is active."""
        if not self._active or self._access_token is None:
            raise RuntimeError("Codex refresh reply lease is not active.")
        return self._access_token

    def __enter__(self) -> Self:
        """Open this decoded reply exactly once."""
        if self._active or self._access_token is None:
            raise RuntimeError("Codex refresh reply lease is unavailable.")
        self._active = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Drop the credential reference."""
        del exception_type, exception, traceback
        self._active = False
        self._access_token = None

    def __repr__(self) -> str:
        """Return a representation without credential material."""
        return "<CodexRefreshReplyLease redacted>"
