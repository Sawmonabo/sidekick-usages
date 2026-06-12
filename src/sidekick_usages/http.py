"""HTTP client with retry/backoff for OAuth-protected GETs.

Generalized from cc-usage.py so any provider can call it. Retries 429
and 5xx with exponential backoff and honors the ``Retry-After``
header. Raises typed :class:`UsageError` subclasses so callers can
react meaningfully.
"""

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, cast

from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
)

#: Regex extracting the scope name from Anthropic's 403 message,
#: e.g. ``"OAuth token does not meet scope requirement user:profile"``.
#: Conservative match: any non-whitespace run after the literal phrase
#: ``scope requirement``. Soft-failure on parse miss is intentional —
#: the rest of the error is still surfaced via ``api_message``.
_SCOPE_REQUIREMENT_RE = re.compile(r"scope requirement (\S+)")

#: Exclusive upper bound for the HTTP 5xx server-error range.
#: :class:`http.HTTPStatus` enumerates only assigned codes (max is
#: ``NETWORK_AUTHENTICATION_REQUIRED = 511``), so a range check
#: against the enum would miss non-standard 5xx codes (e.g., 599
#: from Cloudflare/Heroku timeouts). 600 is the conventional
#: exclusive upper bound for "any 5xx".
SERVER_ERROR_END = 600

#: Cryptographically-strong RNG for retry-jitter. ``random.uniform``
#: would work functionally but bandit flags it (B311) for use in
#: a security-adjacent context; ``SystemRandom`` is the right tool
#: and the cost is negligible for jitter computation.
_JITTER_RNG = secrets.SystemRandom()


class HttpClient:
    """Tiny GET-only HTTP client with retry/backoff."""

    def __init__(
        self,
        max_retries: int = 3,
        base_backoff: float = 1.5,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """:param max_retries: Attempts beyond the first call.

        :param base_backoff: Base for exponential backoff (seconds).
        :param timeout: Per-request socket timeout in seconds.
        :param sleep: Injectable sleep, mainly to keep tests fast.
        """
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.timeout = timeout
        self._sleep = sleep

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Issue a GET request and decode the JSON response.

        :param url: Endpoint URL (must use ``https://``).
        :param headers: Headers to include (Authorization, UA, etc.).
        :return: Decoded JSON payload.
        :raises ValueError: When the URL is not HTTPS.
        :raises AuthError: On HTTP 401.
        :raises ForbiddenError: On HTTP 403 (token authentic but
            unauthorized for this endpoint — e.g. scope missing).
        :raises RateLimitError: On 429 after retries exhausted.
        :raises TransientError: On 5xx or network errors after
            retries exhausted.
        """
        self._require_https(url)
        attempt = 0
        last_retry_after: int | None = None
        while True:
            try:
                return self._request(url, headers)
            except urllib.error.HTTPError as e:
                if e.code == HTTPStatus.UNAUTHORIZED:
                    raise AuthError(
                        "Token expired or invalid (HTTP 401)."
                    ) from e
                if e.code == HTTPStatus.FORBIDDEN:
                    raise self._build_forbidden(e) from e
                if (
                    e.code == HTTPStatus.TOO_MANY_REQUESTS
                    or HTTPStatus.INTERNAL_SERVER_ERROR
                    <= e.code
                    < SERVER_ERROR_END
                ):
                    last_retry_after = self._retry_after(e)
                    if attempt >= self.max_retries:
                        if e.code == HTTPStatus.TOO_MANY_REQUESTS:
                            raise RateLimitError(
                                "Rate limited (HTTP 429) after "
                                f"{attempt + 1} attempts.",
                                retry_after=last_retry_after,
                            ) from e
                        raise TransientError(
                            f"HTTP {e.code} {e.reason} after "
                            f"{attempt + 1} attempts."
                        ) from e
                    self._sleep(self._backoff(attempt, last_retry_after))
                    attempt += 1
                    continue
                raise TransientError(f"HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                if attempt >= self.max_retries:
                    raise TransientError(f"Network error: {e.reason}") from e
                self._sleep(self._backoff(attempt, None))
                attempt += 1

    def post_capture_headers(
        self,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, str]:
        """POST a JSON body and return the response headers.

        Used to probe ``/v1/messages`` for the
        ``anthropic-ratelimit-unified-*`` headers — Claude Code itself
        does the same to populate its rate-limit state without needing
        the ``user:profile`` OAuth scope. The response body is drained
        but discarded; only headers carry the load-bearing data.

        :param url: Endpoint URL (must use ``https://``).
        :param json_body: Dict to JSON-encode as the request body.
        :param headers: Request headers (Authorization, beta, ...).
            ``Content-Type: application/json`` is added automatically.
        :return: Response headers with lowercase keys. Callers should
            use lowercase header names when reading the dict.
        :raises ValueError: When the URL is not HTTPS.
        :raises AuthError: On HTTP 401.
        :raises ForbiddenError: On HTTP 403.
        :raises RateLimitError: On 429 after retries exhausted.
        :raises TransientError: On 5xx or network errors after
            retries exhausted.
        """
        self._require_https(url)
        body_bytes = json.dumps(json_body).encode("utf-8")
        full_headers = {"Content-Type": "application/json", **headers}
        attempt = 0
        last_retry_after: int | None = None
        while True:
            try:
                return self._post_for_headers(url, body_bytes, full_headers)
            except urllib.error.HTTPError as e:
                if e.code == HTTPStatus.UNAUTHORIZED:
                    raise AuthError(
                        "Token expired or invalid (HTTP 401)."
                    ) from e
                if e.code == HTTPStatus.FORBIDDEN:
                    raise self._build_forbidden(e) from e
                if (
                    e.code == HTTPStatus.TOO_MANY_REQUESTS
                    or HTTPStatus.INTERNAL_SERVER_ERROR
                    <= e.code
                    < SERVER_ERROR_END
                ):
                    last_retry_after = self._retry_after(e)
                    if attempt >= self.max_retries:
                        if e.code == HTTPStatus.TOO_MANY_REQUESTS:
                            raise RateLimitError(
                                "Rate limited (HTTP 429) after "
                                f"{attempt + 1} attempts.",
                                retry_after=last_retry_after,
                            ) from e
                        raise TransientError(
                            f"HTTP {e.code} {e.reason} after "
                            f"{attempt + 1} attempts."
                        ) from e
                    self._sleep(self._backoff(attempt, last_retry_after))
                    attempt += 1
                    continue
                raise TransientError(f"HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                if attempt >= self.max_retries:
                    raise TransientError(f"Network error: {e.reason}") from e
                self._sleep(self._backoff(attempt, None))
                attempt += 1

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST application/x-www-form-urlencoded and parse JSON.

        Used for OAuth refresh token exchanges (Codex needs this).

        :param url: Endpoint URL (must use ``https://``).
        :param data: Form fields to send.
        :param headers: Optional extra headers.
        :return: Decoded JSON payload.
        :raises ValueError: When the URL is not HTTPS.
        :raises AuthError: On HTTP 401.
        :raises ForbiddenError: On HTTP 403.
        :raises TransientError: On other errors after retries.
        """
        self._require_https(url)
        body = "&".join(
            f"{k}={urllib.parse.quote(v, safe='')}" for k, v in data.items()
        ).encode("utf-8")
        full_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if headers:
            full_headers.update(headers)
        return self._post_json_bytes(url, body, full_headers)

    def post_json(
        self,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST a JSON body and parse a JSON response.

        Used for OAuth refresh endpoints that follow Claude Code's
        JSON request shape.

        :param url: Endpoint URL (must use ``https://``).
        :param json_body: Dict to JSON-encode as the request body.
        :param headers: Optional extra headers.
        :return: Decoded JSON payload.
        :raises ValueError: When the URL is not HTTPS.
        :raises AuthError: On HTTP 401.
        :raises ForbiddenError: On HTTP 403.
        :raises TransientError: On other errors.
        """
        self._require_https(url)
        body = json.dumps(json_body).encode("utf-8")
        full_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            full_headers.update(headers)
        return self._post_json_bytes(url, body, full_headers)

    # -- internals --------------------------------------------------
    @staticmethod
    def _require_https(url: str) -> None:
        """Reject URLs that use a scheme other than ``https``.

        Defense-in-depth against :func:`urllib.request.urlopen`
        accepting ``file://`` or other schemes (CWE-22).

        :param url: URL to inspect.
        :raises ValueError: When the URL scheme is not ``https``.
        """
        scheme = urllib.parse.urlparse(url).scheme
        if scheme != "https":
            raise ValueError(
                f"Refusing non-HTTPS URL scheme {scheme!r}: {url!r}"
            )

    def _post_json_bytes(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """POST bytes and decode a JSON response, with retry/backoff."""
        attempt = 0
        while True:
            try:
                return self._post_request_json(url, body, headers)
            except urllib.error.HTTPError as e:
                attempt = self._handle_post_http_error(e, attempt)
            except urllib.error.URLError as e:
                if attempt >= self.max_retries:
                    raise TransientError(f"Network error: {e.reason}") from e
                self._sleep(self._backoff(attempt, None))
                attempt += 1

    def _post_request_json(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Issue one byte POST and decode the JSON response."""
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
            return cast("dict[str, Any]", payload)

    def _handle_post_http_error(
        self,
        err: urllib.error.HTTPError,
        attempt: int,
    ) -> int:
        """Raise terminal POST errors or sleep and return next attempt."""
        if err.code == HTTPStatus.UNAUTHORIZED:
            raise AuthError("Refresh rejected (HTTP 401).") from err
        if err.code == HTTPStatus.FORBIDDEN:
            raise self._build_forbidden(err) from err
        if not self._is_retryable_status(err.code):
            raise TransientError(f"HTTP {err.code}: {err.reason}") from err

        retry_after = self._retry_after(err)
        if attempt >= self.max_retries:
            self._raise_exhausted_http_error(err, attempt, retry_after)
        self._sleep(self._backoff(attempt, retry_after))
        return attempt + 1

    @staticmethod
    def _is_retryable_status(code: int) -> bool:
        """Return whether a status should be retried."""
        return (
            code == HTTPStatus.TOO_MANY_REQUESTS
            or HTTPStatus.INTERNAL_SERVER_ERROR <= code < SERVER_ERROR_END
        )

    @staticmethod
    def _raise_exhausted_http_error(
        err: urllib.error.HTTPError,
        attempt: int,
        retry_after: int | None,
    ) -> None:
        """Raise the typed error after all retries are exhausted."""
        if err.code == HTTPStatus.TOO_MANY_REQUESTS:
            raise RateLimitError(
                f"Rate limited (HTTP 429) after {attempt + 1} attempts.",
                retry_after=retry_after,
            ) from err
        raise TransientError(
            f"HTTP {err.code} {err.reason} after {attempt + 1} attempts."
        ) from err

    def _request(
        self,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Issue one HTTP request and return parsed JSON.

        :param url: Endpoint URL.
        :param headers: Request headers.
        :return: Decoded JSON payload.
        """
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
            return cast("dict[str, Any]", payload)

    def _post_for_headers(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, str]:
        """Issue one POST and return response headers, draining body.

        :param url: Endpoint URL.
        :param body: Pre-encoded request body bytes.
        :param headers: Request headers.
        :return: Response headers normalized to lowercase keys.
        """
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            r.read()
            return {k.lower(): v for k, v in r.headers.items()}

    @staticmethod
    def _build_forbidden(
        err: urllib.error.HTTPError,
    ) -> ForbiddenError:
        """Parse a 403 response body into a :class:`ForbiddenError`.

        Anthropic returns a JSON body with a user-facing message
        (e.g. ``"OAuth token does not meet scope requirement
        user:profile"``) that the caller wants to surface to the
        user. When the body is missing or malformed, fall back to
        a generic message — the typed exception is still raised so
        the CLI hard-fails rather than saving an unusable token.

        :param err: The HTTPError carrying the response.
        :return: A populated :class:`ForbiddenError`.
        """
        api_message: str | None = None
        try:
            raw = err.read()
            if raw:
                payload = json.loads(raw.decode("utf-8"))
                # Anthropic envelope: {"error": {"message": "..."}}.
                # Be permissive: also accept top-level "message".
                err_obj = payload.get("error") or {}
                api_message = err_obj.get("message") or payload.get("message")
        except OSError, ValueError, AttributeError:
            api_message = None

        required_scope: str | None = None
        if api_message:
            match = _SCOPE_REQUIREMENT_RE.search(api_message)
            if match:
                required_scope = match.group(1)

        summary = (
            f"HTTP 403 Forbidden: {api_message}"
            if api_message
            else "HTTP 403 Forbidden (no body)."
        )
        return ForbiddenError(
            summary,
            api_message=api_message,
            required_scope=required_scope,
        )

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError) -> int | None:
        """Parse the ``Retry-After`` header if present.

        :param err: The HTTPError carrying response headers.
        :return: Integer seconds, or ``None`` when absent/invalid.
        """
        raw = err.headers.get("Retry-After") if err.headers else None
        if not raw:
            return None
        try:
            return max(0, int(raw))
        except TypeError, ValueError:
            return None

    def _backoff(
        self,
        attempt: int,
        retry_after: int | None,
    ) -> float:
        """Compute a sleep duration before the next attempt.

        :param attempt: Zero-based attempt index that just failed.
        :param retry_after: Server-suggested wait, or ``None``.
        :return: Seconds to sleep.
        """
        if retry_after is not None:
            return float(retry_after)
        delay = self.base_backoff * (2**attempt) + _JITTER_RNG.uniform(
            0.0, 0.25
        )
        return float(delay)
