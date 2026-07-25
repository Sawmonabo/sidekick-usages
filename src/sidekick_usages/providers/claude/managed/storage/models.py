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
from sidekick_usages.providers.claude.models import ClaudeManagedProfile


@dataclass(frozen=True, slots=True)
class ClaudeKeychainTarget:
    """One exact non-secret macOS Keychain lookup target."""

    account: str = field(repr=False)
    service: str


@dataclass(frozen=True, slots=True)
class ClaudeAuthoritySnapshot:
    """Secret-free metadata for one protected credential generation."""

    profile: ClaudeManagedProfile
    executable_version: str
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    plan: str
    scopes: tuple[str, ...]
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    health: CredentialHealth
    action: CredentialAction


class ClaudeProtectedLogin:
    """Operation-scoped refresh input from protected Claude storage."""

    __slots__ = ("_active", "_refresh_token", "_scopes", "_snapshot")

    def __init__(
        self,
        snapshot: ClaudeAuthoritySnapshot,
        refresh_token: str,
        scopes: tuple[str, ...],
    ) -> None:
        self._snapshot = snapshot
        self._refresh_token: str | None = refresh_token
        self._scopes: tuple[str, ...] | None = scopes
        self._active = False

    @property
    def snapshot(self) -> ClaudeAuthoritySnapshot:
        """Return the validated secret-free authority snapshot."""
        return self._snapshot

    @property
    def refresh_token(self) -> str:
        """Return refresh material only while this lease is active."""
        if not self._active or self._refresh_token is None:
            raise RuntimeError("Claude protected login lease is not active.")
        return self._refresh_token

    @property
    def scopes(self) -> tuple[str, ...]:
        """Return OAuth scopes only while this lease is active."""
        if not self._active or self._scopes is None:
            raise RuntimeError("Claude protected login lease is not active.")
        return self._scopes

    def __enter__(self) -> Self:
        """Open this protected login projection exactly once."""
        if (
            self._active
            or self._refresh_token is None
            or self._scopes is None
        ):
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
        self._refresh_token = None
        self._scopes = None

    def __repr__(self) -> str:
        """Return a representation without credential material."""
        return "<ClaudeProtectedLogin redacted>"
