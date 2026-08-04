"""Single-use protected Claude authority projection."""

from sidekick_usages.providers.claude.structured.codec import (
    ClaudeProtectedChannelError,
    clear_secret_buffer,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
)


class ClaudeProtectedOAuthFrame:
    """Single-use mutable OAuth projection bound to one target epoch."""

    __slots__ = ("_active", "_observed", "protected_binding")

    def __init__(
        self,
        binding: ClaudeStructuredBinding,
        oauth: bytearray,
    ) -> None:
        if not oauth:
            raise ClaudeProtectedChannelError(
                "The protected OAuth projection is empty."
            )
        self.protected_binding = binding
        self._active: bytearray | None = oauth
        self._observed = oauth

    @property
    def is_cleared(self) -> bool:
        """Return whether the mutable credential buffer was wiped."""
        return not any(self._observed)

    def take_protected_oauth(self) -> bytearray:
        """Transfer the mutable credential buffer exactly once."""
        oauth = self._active
        if oauth is None:
            raise ClaudeProtectedChannelError(
                "The protected OAuth projection was already consumed."
            )
        self._active = None
        return oauth

    def close_protected_frame(self) -> None:
        """Clear any credential buffer still owned by this frame."""
        if self._active is not None:
            clear_secret_buffer(self._active)
            self._active = None

    def __repr__(self) -> str:
        """Return no authority or credential material."""
        return "<ClaudeProtectedOAuthFrame redacted>"
