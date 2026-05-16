"""Typed exception hierarchy.

Errors are raised by providers, the HTTP client, and the credential
detector, then caught at the CLI boundary where they are rendered
into per-account error blocks.
"""


class UsageError(Exception):
    """Base for all errors this program raises and renders itself."""


class AuthError(UsageError):
    """The token was rejected (HTTP 401). User must re-login."""


class ForbiddenError(UsageError):
    """The token is authentic but lacks the required scope (HTTP 403).

    Distinct from :class:`AuthError` (401) because the token itself
    is genuine — the API recognized it, then refused this specific
    request. The most common cause for Claude is pasting a token
    from ``claude setup-token`` (scoped narrowly for inference) into
    an endpoint that needs the broader scopes granted by an
    interactive ``claude /login``.

    :ivar api_message: User-facing message from the API error body
        when one was returned, otherwise ``None``.
    :ivar required_scope: Scope name the API said was missing
        (parsed from ``api_message``), otherwise ``None``.
    """

    def __init__(
        self,
        message: str,
        api_message: str | None = None,
        required_scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.api_message = api_message
        self.required_scope = required_scope


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
