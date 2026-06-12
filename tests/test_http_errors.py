"""Tests for HTTP-status-to-exception mapping in :mod:`http`.

These pin the 403 (Forbidden) handling that distinguishes
scope/permission failures from generic transient errors. Anthropic's
``/api/oauth/usage`` returns 403 with a body like::

    {"type":"error","error":{"type":"permission_error",
     "message":"OAuth token does not meet scope requirement
                user:profile"}}

The CLI must (a) raise :class:`ForbiddenError` (not
:class:`TransientError`) so the ``add`` subcommand can hard-fail
instead of saving an unusable token, and (b) carry both the
user-facing API message and the parsed required-scope so the CLI can
guide the user to ``claude /login``.
"""

import io
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from unittest.mock import patch

import pytest

from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
)
from sidekick_usages.http import HttpClient

POST_JSON_RETRY_CALLS = 2
RETRY_AFTER_SECONDS = 7


def _http_error(
    code: int,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    """Build a ``urllib.error.HTTPError`` carrying a JSON body.

    :param code: HTTP status code to embed.
    :param body: Optional JSON payload; serialized into the
        response stream so ``err.read()`` returns it.
    :param headers: Optional response headers, copied into the
        :class:`email.message.Message` that
        :class:`urllib.error.HTTPError` expects.
    :return: An HTTPError ready to be raised by a mock.
    """
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(
        url="https://api.anthropic.com/api/oauth/usage",
        code=code,
        msg=f"HTTP {code}",
        hdrs=msg,
        fp=io.BytesIO(raw),
    )


@pytest.fixture
def client() -> HttpClient:
    """Build an :class:`HttpClient` with retries disabled.

    :return: Client that fails fast (one attempt) and never sleeps.
    """
    return HttpClient(max_retries=0, sleep=lambda _: None)


@pytest.fixture
def patched_urlopen() -> Iterator:
    """Patch ``urllib.request.urlopen`` inside :mod:`http`.

    :yield: The mock for the test to configure.
    """
    with patch("sidekick_usages.http.urllib.request.urlopen") as m:
        yield m


def test_403_raises_forbidden_with_parsed_message(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """403 → :class:`ForbiddenError` carrying API message + scope."""
    patched_urlopen.side_effect = _http_error(
        403,
        {
            "type": "error",
            "error": {
                "type": "permission_error",
                "message": (
                    "OAuth token does not meet scope requirement user:profile"
                ),
            },
        },
    )
    with pytest.raises(ForbiddenError) as exc:
        client.get_json("https://api.anthropic.com/x", {})
    assert exc.value.required_scope == "user:profile"
    assert exc.value.api_message is not None
    assert "user:profile" in exc.value.api_message
    assert "403" in str(exc.value)


def test_403_with_no_body_still_raises_forbidden(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """403 without a body still hard-fails as :class:`ForbiddenError`.

    Defensive: the API contract may shift, but the status code is
    canonical. We must never demote a 403 to ``TransientError``.
    """
    patched_urlopen.side_effect = _http_error(403, body=None)
    with pytest.raises(ForbiddenError) as exc:
        client.get_json("https://api.anthropic.com/x", {})
    assert exc.value.api_message is None
    assert exc.value.required_scope is None


def test_403_with_unparsable_body_falls_back_cleanly(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """Non-JSON 403 body does not crash; the exception still surfaces."""
    err = urllib.error.HTTPError(
        url="https://api.anthropic.com/x",
        code=403,
        msg="HTTP 403",
        hdrs=Message(),
        fp=io.BytesIO(b"<html>nope</html>"),
    )
    patched_urlopen.side_effect = err
    with pytest.raises(ForbiddenError) as exc:
        client.get_json("https://api.anthropic.com/x", {})
    assert exc.value.api_message is None


def test_403_alternative_scope_name_is_extracted(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """Scope regex matches any non-whitespace token after the phrase."""
    patched_urlopen.side_effect = _http_error(
        403,
        {
            "error": {
                "message": (
                    "OAuth token does not meet scope "
                    "requirement org:admin:read"
                )
            }
        },
    )
    with pytest.raises(ForbiddenError) as exc:
        client.get_json("https://api.anthropic.com/x", {})
    assert exc.value.required_scope == "org:admin:read"


def test_401_still_raises_auth_error(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """401 routing is unchanged by the 403 work."""
    patched_urlopen.side_effect = _http_error(401)
    with pytest.raises(AuthError):
        client.get_json("https://api.anthropic.com/x", {})


def test_500_still_raises_transient_error(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """5xx routing is unchanged: retries exhaust into TransientError."""
    patched_urlopen.side_effect = _http_error(500)
    with pytest.raises(TransientError):
        client.get_json("https://api.anthropic.com/x", {})


class _JsonResponse:
    """Minimal context-manager response for POST JSON tests."""

    def __init__(self, payload: dict[str, object]) -> None:
        """:param payload: JSON payload returned by ``read``."""
        self._payload = payload

    def __enter__(self) -> _JsonResponse:
        """:return: Self, matching urllib response context managers."""
        return self

    def __exit__(self, *args: object) -> None:
        """:param args: Unused exception triple."""

    def read(self) -> bytes:
        """:return: Encoded JSON response body."""
        return json.dumps(self._payload).encode("utf-8")


def test_post_json_sends_json_body_and_headers(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """``post_json`` sends a JSON request and decodes JSON response."""
    patched_urlopen.return_value = _JsonResponse({"access_token": "new"})

    response = client.post_json(
        "https://api.anthropic.com/v1/oauth/token",
        json_body={"grant_type": "refresh_token"},
        headers={"anthropic-beta": "oauth-2025-04-20"},
    )

    assert response == {"access_token": "new"}
    request = patched_urlopen.call_args.args[0]
    assert isinstance(request, urllib.request.Request)
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert json.loads(request.data.decode("utf-8")) == {
        "grant_type": "refresh_token"
    }


def test_post_json_401_raises_auth_error(
    client: HttpClient,
    patched_urlopen,
) -> None:
    """401 from a JSON OAuth POST is still an auth failure."""
    patched_urlopen.side_effect = _http_error(401)
    with pytest.raises(AuthError):
        client.post_json(
            "https://api.anthropic.com/v1/oauth/token",
            json_body={"grant_type": "refresh_token"},
        )


def test_post_json_retries_429_before_returning(
    patched_urlopen,
) -> None:
    """``post_json`` retries rate-limited token refresh requests."""
    sleeps: list[float] = []
    client = HttpClient(max_retries=1, sleep=sleeps.append)
    patched_urlopen.side_effect = [
        _http_error(429, headers={"Retry-After": "0"}),
        _JsonResponse({"access_token": "new"}),
    ]

    response = client.post_json(
        "https://api.anthropic.com/v1/oauth/token",
        json_body={"grant_type": "refresh_token"},
    )

    assert response == {"access_token": "new"}
    assert patched_urlopen.call_count == POST_JSON_RETRY_CALLS
    assert sleeps == [0.0]


def test_post_json_429_exhausts_to_rate_limit_error(
    patched_urlopen,
) -> None:
    """``post_json`` preserves rate-limit semantics after retries."""
    client = HttpClient(max_retries=0, sleep=lambda _: None)
    patched_urlopen.side_effect = _http_error(
        429,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )

    with pytest.raises(RateLimitError) as exc:
        client.post_json(
            "https://api.anthropic.com/v1/oauth/token",
            json_body={"grant_type": "refresh_token"},
        )

    assert exc.value.retry_after == RETRY_AFTER_SECONDS
