"""Protected Claude storage models."""

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Self

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
)
from sidekick_usages.core.models import ClaudeLoginCredentials
from sidekick_usages.providers.claude.types import ClaudeProfile


@dataclass(frozen=True, slots=True)
class ClaudeKeychainTarget:
    """One exact non-secret macOS Keychain lookup target."""

    account: str = field(repr=False)
    service: str


@dataclass(frozen=True, slots=True)
class ClaudeAuthoritySnapshot:
    """Secret-free metadata for one protected credential generation."""

    profile: ClaudeProfile
    executable_version: str
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    plan: str
    scopes: tuple[str, ...]
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    health: CredentialHealth
    action: CredentialAction


@dataclass(frozen=True, slots=True)
class ClaudeCredentialObservation:
    """Credential generation retained without requiring provider identity."""

    generation: AuthorityGeneration
    health: CredentialHealth
    action: CredentialAction
    snapshot: ClaudeAuthoritySnapshot | None = None

    def __post_init__(self) -> None:
        """Require a complete snapshot to agree with retained metadata."""
        if self.snapshot is not None and (
            self.snapshot.generation != self.generation
            or self.snapshot.health is not self.health
            or self.snapshot.action is not self.action
        ):
            raise ValueError("Claude credential observation is inconsistent.")


class ClaudeProtectedLogin:
    """Operation-scoped login credentials from protected Claude storage."""

    __slots__ = ("_active", "_credentials", "_snapshot")

    def __init__(
        self,
        snapshot: ClaudeAuthoritySnapshot,
        credentials: ClaudeLoginCredentials,
    ) -> None:
        self._snapshot = snapshot
        self._credentials: ClaudeLoginCredentials | None = credentials
        self._active = False

    @property
    def snapshot(self) -> ClaudeAuthoritySnapshot:
        """Return the validated secret-free authority snapshot."""
        return self._snapshot

    @property
    def refresh_token(self) -> str:
        """Return refresh material only while this lease is active."""
        return self.credentials.refresh_token

    @property
    def scopes(self) -> tuple[str, ...]:
        """Return OAuth scopes only while this lease is active."""
        return self.credentials.scopes

    @property
    def credentials(self) -> ClaudeLoginCredentials:
        """Return complete credentials only while this lease is active."""
        if not self._active or self._credentials is None:
            raise RuntimeError("Claude protected login lease is not active.")
        return self._credentials

    def __enter__(self) -> Self:
        """Open this protected login projection exactly once."""
        if self._active or self._credentials is None:
            raise RuntimeError(
                "Claude protected login lease is not available."
            )
        self._active = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release every credential reference."""
        del exception_type, exception, traceback
        self._active = False
        self._credentials = None

    def __repr__(self) -> str:
        """Return a representation without credential material."""
        return "<ClaudeProtectedLogin redacted>"
