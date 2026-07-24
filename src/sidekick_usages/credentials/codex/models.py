"""Secret-free results for managed Codex authority operations."""

from dataclasses import dataclass
from types import TracebackType
from typing import Self

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.providers.codex.models import CodexAuthSnapshot


class CodexProjectionLease:
    """Short-lived access token bound to one proven managed authority."""

    __slots__ = (
        "_access_token",
        "_account_id",
        "_active",
        "_generation",
        "_plan",
        "_provider_identity",
    )

    def __init__(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
        plan: str,
        access_token: str,
    ) -> None:
        self._account_id = account_id
        self._provider_identity = provider_identity
        self._generation = generation
        self._plan = plan
        self._access_token: str | None = access_token
        self._active = False

    @property
    def account_id(self) -> SidekickAccountId:
        """Return the stable Sidekick account identifier."""
        return self._account_id

    @property
    def provider_identity(self) -> ProviderIdentity:
        """Return the locally proven provider identity."""
        return self._provider_identity

    @property
    def generation(self) -> AuthorityGeneration:
        """Return the protected provider generation."""
        return self._generation

    @property
    def plan(self) -> str:
        """Return the validated provider plan."""
        return self._plan

    @property
    def access_token(self) -> str:
        """Return the credential only while this lease is active."""
        if not self._active or self._access_token is None:
            raise RuntimeError("Codex projection lease is not active.")
        return self._access_token

    def __enter__(self) -> Self:
        """Open this projection exactly once."""
        if self._active or self._access_token is None:
            raise RuntimeError("Codex projection lease is not available.")
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
        self._access_token = None

    def __repr__(self) -> str:
        """Return a representation without credential material."""
        return "<CodexProjectionLease redacted>"


@dataclass(frozen=True, slots=True)
class CodexAuthorityExpectation:
    """Saved identity, authority, and optional generation baseline."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity
    baseline: CodexAuthSnapshot | None

    def __post_init__(self) -> None:
        """Reject a baseline that belongs to another provider identity."""
        if (
            self.baseline is not None
            and self.baseline.provider_identity != self.provider_identity
        ):
            raise ValueError("Codex authority baseline identity is invalid.")


@dataclass(frozen=True, slots=True)
class CodexManagedAuthorityResult:
    """One persisted managed-account outcome containing no credentials."""

    outcome: CodexManagedOutcome
    account: SavedAccount

    def __post_init__(self) -> None:
        """Require a managed Codex account result."""
        if (
            self.account.provider_id is not ProviderId.CODEX
            or not self.account.has_managed_authority
        ):
            raise ValueError("Managed Codex result account is invalid.")
