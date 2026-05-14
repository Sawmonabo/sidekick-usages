"""Typed exception hierarchy.

Errors are raised by providers, the HTTP client, and the credential
detector, then caught at the CLI boundary where they are rendered
into per-account error blocks.
"""


class UsageError(Exception):
    """Base for all errors this program raises and renders itself."""


class AuthError(UsageError):
    """The token was rejected (HTTP 401). User must re-login."""


class RateLimitError(UsageError):
    """The API returned 429 even after retries.

    :ivar retry_after: Seconds the server asked us to wait, or None.
    """

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(UsageError):
    """A 5xx or network failure that persisted across retries."""


class UnsupportedOperationError(UsageError):
    """Provider does not support the requested operation.

    Raised, for example, when the user runs ``setup-token codex`` —
    OpenAI has no analogue to ``claude setup-token``.
    """
