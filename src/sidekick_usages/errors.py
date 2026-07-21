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


class InsecureUrlError(UsageError):
    """An HTTP operation targeted a forbidden URL scheme."""

    def __init__(self) -> None:
        super().__init__("HTTP requests require an HTTPS URL.")


class InvalidPayloadError(UsageError):
    """An HTTP request or response payload failed its boundary."""

    def __init__(self) -> None:
        super().__init__(
            "HTTP payload is invalid or exceeds its allowed size."
        )


class ProviderIdentityError(UsageError):
    """A provider request cannot resolve its required saved identity."""


class HttpStatusError(UsageError):
    """An HTTP response had a permanent, non-auth failure status.

    :param status_code: Numeric response status without response details.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP request failed with status {status_code}.")
        self.status_code = status_code


class UnsupportedOperationError(UsageError):
    """Provider does not support the requested operation.

    Raised, for example, when the user runs ``setup-token codex`` —
    OpenAI has no analogue to ``claude setup-token``.
    """
