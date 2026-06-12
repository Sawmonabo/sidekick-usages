"""Tests for ClaudeProvider's header-path usage fetch.

The header path mirrors Claude Code's ``de1()`` / ``UgK()`` startup
probe (located around byte offset 220 989 000 in the Bun-bundled
binary): POST a 1-token request to ``/v1/messages`` and read the
``anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}`` response
headers. This is what makes ``claude setup-token`` outputs usable —
they have ``user:inference`` (enough to call ``/v1/messages``) but
lack ``user:profile`` (so ``/api/oauth/usage`` returns 403).
"""

from typing import Any

from sidekick_usages.http import HttpClient
from sidekick_usages.providers.claude import (
    ANTHROPIC_BETA,
    MESSAGES_URL,
    PROBE_MODEL,
    USAGE_URL,
    ClaudeProvider,
)
from sidekick_usages.store import Account

#: Reference utilization values quoted verbatim from the unified
#: rate-limit headers in ``anthropics/claude-code`` issue #12829.
_REF_5H_UTILIZATION = 0.0184
_REF_7D_UTILIZATION = 0.737
_REF_5H_UTILIZATION_PERCENT = 1.84
_REF_7D_UTILIZATION_PERCENT = 73.7


class _FakeHttp(HttpClient):
    """Records calls and returns canned data for both HTTP methods.

    Inherits from :class:`HttpClient` so the static checker accepts it
    as the ``http`` argument to provider methods. The base
    ``__init__`` is called with defaults; the canned-response state is
    added on top.
    Mocking at this boundary keeps these tests out of the urllib
    layer (covered by :mod:`test_http_errors` instead).
    """

    def __init__(
        self,
        response_headers: dict[str, str] | None = None,
        response_json: dict[str, Any] | None = None,
    ) -> None:
        """:param response_headers: Canned headers for POST mock.

        :param response_json: Canned body for GET mock.
        """
        super().__init__()
        self.response_headers = response_headers or {}
        self.response_json = response_json or {}
        self.calls: list[tuple[str, str]] = []
        self.last_post_body: dict[str, Any] | None = None
        self.last_post_headers: dict[str, str] | None = None

    def post_capture_headers(
        self,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, str]:
        """Stand-in for :meth:`HttpClient.post_capture_headers`."""
        self.calls.append(("POST", url))
        self.last_post_body = json_body
        self.last_post_headers = headers
        return self.response_headers

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Stand-in for :meth:`HttpClient.get_json`."""
        del headers
        self.calls.append(("GET", url))
        return self.response_json


def _acct(scopes: list[str] | None) -> Account:
    """Build a minimal Account fixture.

    :param scopes: Scope list (or None) to assign.
    :return: Account with sentinel fields.
    """
    return Account(
        label="t",
        provider_id="claude",
        access_token="sk-ant-oat01-test",
        scopes=scopes,
    )


#: Sample mid-window response. Numbers match the verbatim
#: ``anthropic-ratelimit-unified-*`` values quoted in
#: ``anthropics/claude-code`` issue #12829.
_LIVE_HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.0184",
    "anthropic-ratelimit-unified-5h-reset": "1778915400",
    "anthropic-ratelimit-unified-7d-utilization": "0.737",
    "anthropic-ratelimit-unified-7d-reset": "1779192000",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-status": "allowed",
}


# -- _fetch_via_headers: request shape ----------------------------
def test_fetch_via_headers_targets_messages_endpoint() -> None:
    """The probe POSTs to ``/v1/messages``, not ``/api/oauth/usage``."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert http.calls == [("POST", MESSAGES_URL)]


def test_fetch_via_headers_sends_bearer_auth_and_beta() -> None:
    """Bearer auth + ``oauth-2025-04-20`` beta header are required.

    Empirically: ``x-api-key`` returns 401, ``Authorization: Bearer``
    returns 200 on ``/v1/messages`` for ``sk-ant-oat01-`` tokens.
    """
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    acct = _acct([])
    acct.access_token = "sk-ant-oat01-secret"
    ClaudeProvider()._fetch_via_headers(acct, http)
    assert http.last_post_headers is not None
    assert (
        http.last_post_headers["Authorization"] == "Bearer sk-ant-oat01-secret"
    )
    assert http.last_post_headers["anthropic-beta"] == ANTHROPIC_BETA


def test_fetch_via_headers_sends_one_token_probe_body() -> None:
    """Body uses ``max_tokens=1`` against a small model.

    Matches Claude Code's ``de1()`` shape so the request looks like
    normal Claude Code traffic — most stable surface against future
    server-side changes.
    """
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert http.last_post_body == {
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "quota"}],
    }


# -- _fetch_via_headers: header parsing ---------------------------
def test_fetch_via_headers_parses_5h_and_7d_windows() -> None:
    """Header-path fractions are normalized to display percentages."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    report = ClaudeProvider()._fetch_via_headers(_acct([]), http)
    names = {w.name: w for w in report.windows}
    assert set(names) == {"5h", "7d"}
    assert round(names["5h"].utilization, 2) == _REF_5H_UTILIZATION_PERCENT
    assert round(names["7d"].utilization, 1) == _REF_7D_UTILIZATION_PERCENT
    assert names["5h"].resets_at is not None
    assert names["5h"].resets_at.endswith("+00:00")


def test_fetch_via_headers_omits_window_when_headers_missing() -> None:
    """Missing 5h headers omit the 5h window — don't synthesize zeros."""
    headers = {k: v for k, v in _LIVE_HEADERS.items() if "-5h-" not in k}
    http = _FakeHttp(response_headers=headers)
    report = ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert [w.name for w in report.windows] == ["7d"]


def test_fetch_via_headers_returns_empty_windows_on_empty_response() -> None:
    """No unified headers → empty windows list, no crash.

    Defensive: the unified-* family is undocumented. If Anthropic
    renames or removes the headers, fetch should degrade to an
    empty report (renderer shows no bars) rather than throwing.
    """
    http = _FakeHttp(response_headers={})
    report = ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert report.windows == []


def test_fetch_via_headers_skips_non_numeric_utilization() -> None:
    """Garbage utilization value skips that window, no crash."""
    headers = {
        **_LIVE_HEADERS,
        "anthropic-ratelimit-unified-5h-utilization": "not-a-float",
    }
    http = _FakeHttp(response_headers=headers)
    report = ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert [w.name for w in report.windows] == ["7d"]


def test_fetch_via_headers_skips_non_numeric_reset() -> None:
    """Garbage reset value skips that window, no crash."""
    headers = {
        **_LIVE_HEADERS,
        "anthropic-ratelimit-unified-7d-reset": "tomorrow",
    }
    http = _FakeHttp(response_headers=headers)
    report = ClaudeProvider()._fetch_via_headers(_acct([]), http)
    assert [w.name for w in report.windows] == ["5h"]


# -- fetch_usage: scope-based routing -----------------------------
def test_fetch_usage_routes_inference_only_to_header_path() -> None:
    """scopes lacking ``user:profile`` → header path."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    ClaudeProvider().fetch_usage(_acct(["user:inference"]), http)
    assert http.calls == [("POST", MESSAGES_URL)]


def test_fetch_usage_routes_empty_scopes_to_header_path() -> None:
    """scopes=[] (self-heal sentinel) → header path."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    ClaudeProvider().fetch_usage(_acct([]), http)
    assert http.calls == [("POST", MESSAGES_URL)]


def test_fetch_usage_routes_full_scope_to_oauth_path() -> None:
    """scopes including ``user:profile`` → ``/api/oauth/usage``."""
    http = _FakeHttp(
        response_json={
            "five_hour": {
                "utilization": 0.1,
                "resets_at": "2026-05-16T00:00:00Z",
            },
        }
    )
    ClaudeProvider().fetch_usage(
        _acct(["user:profile", "user:inference"]),
        http,
    )
    assert http.calls == [("GET", USAGE_URL)]


def test_fetch_usage_routes_unknown_scope_to_oauth_path() -> None:
    """scopes=None (never observed) → optimistic OAuth path.

    The CLI catches any resulting 403, self-heals scopes=[], and
    retries via the header path. See ``_handle_runtime_forbidden``.
    """
    http = _FakeHttp(response_json={})
    ClaudeProvider().fetch_usage(_acct(None), http)
    assert http.calls == [("GET", USAGE_URL)]
