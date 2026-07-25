"""Provider models for managed Codex account and login boundaries."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.generation import CodexGenerationOrder

_DEFAULT_HTTPS_PORT = 443
_OFFICIAL_LOGIN_HOSTS = frozenset({"auth.openai.com"})


@dataclass(frozen=True, slots=True)
class CodexAuthSnapshot:
    """Validated identity and generation from one protected Codex home."""

    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    generation_order: CodexGenerationOrder = field(repr=False)
    plan: str

    def __post_init__(self) -> None:
        """Validate safe metadata and the provider generation ordering key."""
        if any(value < 0 for value in self.generation_order):
            raise ValueError("Codex generation order is invalid.")
        require_bounded_text(
            self.plan,
            name="Codex plan",
            maximum=MAX_METADATA_BYTES,
        )

    def advanced_from(self, previous: CodexAuthSnapshot) -> bool:
        """Return whether this same-account generation is newer."""
        return (
            self.provider_identity == previous.provider_identity
            and self.generation_order > previous.generation_order
        )

    def not_older_than(self, generation: CodexAuthSnapshot) -> bool:
        """Return whether this same-account generation did not regress."""
        return (
            self.provider_identity == generation.provider_identity
            and self.generation_order >= generation.generation_order
        )


@dataclass(frozen=True, slots=True)
class CodexAccountObservation:
    """Sanitized non-null ChatGPT account observation."""

    plan: str

    def __post_init__(self) -> None:
        """Require one bounded provider plan."""
        require_bounded_text(
            self.plan,
            name="Codex plan",
            maximum=MAX_METADATA_BYTES,
        )


@dataclass(frozen=True, slots=True)
class CodexTokenClaims:
    """Validated identity-bearing claims from one Codex access token."""

    expiry_seconds: int | None
    provider_identity: ProviderIdentity | None
    plan: str | None


@dataclass(frozen=True, slots=True)
class CodexLoginEvent:
    """Ephemeral official-login step safe for direct user presentation."""

    authorization_url: str = field(repr=False)
    user_code: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Require a bounded HTTPS login step without embedded credentials."""
        require_bounded_text(
            self.authorization_url,
            name="Codex authorization URL",
            maximum=MAX_OPAQUE_BYTES,
        )
        if self.user_code is not None:
            require_bounded_text(
                self.user_code,
                name="Codex device code",
                maximum=MAX_METADATA_BYTES,
            )
        try:
            parsed = urlsplit(self.authorization_url)
            port = parsed.port
        except ValueError:
            raise ValueError("Codex authorization URL is invalid.") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _OFFICIAL_LOGIN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and port != _DEFAULT_HTTPS_PORT)
        ):
            raise ValueError("Codex authorization URL is invalid.")


@dataclass(frozen=True, slots=True)
class CodexLoginAttempt:
    """Internal correlation state for one official Codex login."""

    login_id: str = field(repr=False)
    event: CodexLoginEvent = field(repr=False)

    def __post_init__(self) -> None:
        """Require one bounded opaque app-server login identifier."""
        require_bounded_text(
            self.login_id,
            name="Codex login ID",
            maximum=MAX_OPAQUE_BYTES,
        )
