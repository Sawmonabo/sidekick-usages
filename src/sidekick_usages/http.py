"""HTTP client with retry/backoff for OAuth-protected GETs.

Generalized from cc-usage.py so any provider can call it. Retries 429
and 5xx with exponential backoff and honors the ``Retry-After``
header. Raises typed :class:`UsageError` subclasses so callers can
react meaningfully.
"""

import json
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
    RateLimitError,
    TransientError,
)

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
        req = urllib.request.Request(
            url,
            data=body,
            headers=full_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req,
                timeout=self.timeout,
            ) as r:
                payload = json.loads(r.read().decode("utf-8"))
                return cast("dict[str, Any]", payload)
        except urllib.error.HTTPError as e:
            if e.code == HTTPStatus.UNAUTHORIZED:
                raise AuthError("Refresh rejected (HTTP 401).") from e
            raise TransientError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise TransientError(f"Network error: {e.reason}") from e

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
