"""Validated models for one official Codex login."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)

DEFAULT_HTTPS_PORT = 443
OFFICIAL_LOGIN_HOSTS = frozenset({"auth.openai.com"})


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
            or parsed.hostname not in OFFICIAL_LOGIN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and port != DEFAULT_HTTPS_PORT)
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
