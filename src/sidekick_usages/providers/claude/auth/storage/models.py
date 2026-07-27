"""Protected Claude storage and proof models."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
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

_NANOSECONDS_PER_MILLISECOND = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ClaudeKeychainTarget:
    """One exact non-secret macOS Keychain lookup target."""

    account: str = field(repr=False)
    service: str


@dataclass(frozen=True, slots=True)
class ClaudeCredentialPayload:
    """Bounded provider credential bytes with optional file provenance."""

    data: bytes = field(repr=False)
    modified_nanoseconds: int | None = None

    def __post_init__(self) -> None:
        """Reject an invalid provider modification timestamp."""
        if (
            self.modified_nanoseconds is not None
            and self.modified_nanoseconds < 0
        ):
            raise ValueError("Claude credential timestamp is invalid.")


@dataclass(frozen=True, slots=True)
class ClaudeProtectedCredentialSnapshot:
    """Secret-free metadata from one protected credential record."""

    profile: ClaudeProfile
    executable_version: str
    generation: AuthorityGeneration
    plan: str
    scopes: tuple[str, ...]
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    health: CredentialHealth
    action: CredentialAction
    modified_milliseconds: Decimal | None = None

    def __post_init__(self) -> None:
        """Reject invalid provider-visible modification evidence."""
        modified = self.modified_milliseconds
        if modified is not None and (
            not isinstance(modified, Decimal)
            or not modified.is_finite()
            or modified < 0
        ):
            raise ValueError("Claude credential mtimeMs is invalid.")

    def associated_with(
        self,
        provider_identity: ProviderIdentity,
    ) -> ClaudeAuthoritySnapshot:
        """Bind this protected evidence to one status association."""
        return ClaudeAuthoritySnapshot(
            profile=self.profile,
            executable_version=self.executable_version,
            generation=self.generation,
            plan=self.plan,
            scopes=self.scopes,
            access_expires_at=self.access_expires_at,
            refresh_expires_at=self.refresh_expires_at,
            health=self.health,
            action=self.action,
            modified_milliseconds=self.modified_milliseconds,
            provider_identity=provider_identity,
        )


@dataclass(frozen=True, slots=True)
class ClaudeAuthoritySnapshot(ClaudeProtectedCredentialSnapshot):
    """One exact association bound to protected request authority."""

    provider_identity: ProviderIdentity = field(kw_only=True)


class ClaudeProtectedCredential:
    """Operation-scoped credentials read from protected Claude storage."""

    __slots__ = ("_active", "_credentials", "_snapshot")

    def __init__(
        self,
        snapshot: ClaudeProtectedCredentialSnapshot,
        credentials: ClaudeLoginCredentials,
    ) -> None:
        self._snapshot = snapshot
        self._credentials: ClaudeLoginCredentials | None = credentials
        self._active = False

    @property
    def snapshot(self) -> ClaudeProtectedCredentialSnapshot:
        """Return the validated secret-free credential snapshot."""
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
            raise RuntimeError(
                "Claude protected credential lease is not active."
            )
        return self._credentials

    def __enter__(self) -> Self:
        """Open this protected credential projection exactly once."""
        if self._active or self._credentials is None:
            raise RuntimeError(
                "Claude protected credential lease is not available."
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
        return "<ClaudeProtectedCredential redacted>"


class ClaudeProtectedLogin:
    """One proven association with an active protected credential lease."""

    __slots__ = ("_credential", "_snapshot")

    def __init__(
        self,
        snapshot: ClaudeAuthoritySnapshot,
        credential: ClaudeProtectedCredential,
    ) -> None:
        self._snapshot = snapshot
        self._credential = credential

    @property
    def snapshot(self) -> ClaudeAuthoritySnapshot:
        """Return the complete secret-free authority snapshot."""
        return self._snapshot

    @property
    def refresh_token(self) -> str:
        """Return refresh material only while the inner lease is active."""
        return self._credential.refresh_token

    @property
    def scopes(self) -> tuple[str, ...]:
        """Return OAuth scopes only while the inner lease is active."""
        return self._credential.scopes

    @property
    def credentials(self) -> ClaudeLoginCredentials:
        """Return credentials only while the inner lease is active."""
        return self._credential.credentials

    def __repr__(self) -> str:
        """Return a representation without credential material."""
        return "<ClaudeProtectedLogin redacted>"


def provider_mtime_milliseconds(
    modified_nanoseconds: int | None,
) -> Decimal | None:
    """Convert descriptor-qualified nanoseconds to exact ``mtimeMs``."""
    if modified_nanoseconds is None:
        return None
    return Decimal(modified_nanoseconds) / _NANOSECONDS_PER_MILLISECOND
