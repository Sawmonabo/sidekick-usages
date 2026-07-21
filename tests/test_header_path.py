"""Tests for ClaudeProvider's header-path usage fetch.

The header path mirrors Claude Code's ``de1()`` / ``UgK()`` startup
probe (located around byte offset 220 989 000 in the Bun-bundled
binary): POST a 1-token request to ``/v1/messages`` and read the
``anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}`` response
headers. This is what makes ``claude setup-token`` outputs usable —
they have ``user:inference`` (enough to call ``/v1/messages``) but
lack ``user:profile`` (so ``/api/oauth/usage`` returns 403).
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import AccountLabel, HeartbeatStatus
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.claude.heartbeat import ClaudeHeartbeat
from sidekick_usages.providers.claude.usage import (
    ANTHROPIC_BETA,
    MESSAGES_URL,
    PROBE_MODEL,
    USAGE_URL,
)
from sidekick_usages.serialization import JsonObject
from tests.test_support import FixedClock

#: Reference utilization values quoted verbatim from the unified
#: rate-limit headers in ``anthropics/claude-code`` issue #12829.
_REF_5H_UTILIZATION = 0.0184
_REF_7D_UTILIZATION = 0.737
_REF_5H_UTILIZATION_PERCENT = 1.84
_REF_7D_UTILIZATION_PERCENT = 73.7
_REF_5H_RESET_AT = datetime.fromtimestamp(1778915400, tz=UTC)
_OAUTH_RESET_AT = datetime(2026, 6, 12, 18, tzinfo=UTC)


def _provider() -> ClaudeProvider:
    return ClaudeProvider(FixedClock())


class _FakeHttp(HttpClient):
    """Records calls and returns canned data for both HTTP methods.

    Inherits from :class:`HttpClient` so the static checker accepts it
    as the ``http`` argument to provider methods. The base
    ``__init__`` is called with defaults; the canned-response state is
    added on top.
    Mocking at this boundary keeps these provider tests transport-agnostic.
    """

    def __init__(
        self,
        response_headers: dict[str, str] | None = None,
        response_json: JsonObject | None = None,
    ) -> None:
        """:param response_headers: Canned headers for POST mock.

        :param response_json: Canned body for GET mock.
        """
        super().__init__()
        self.response_headers = response_headers or {}
        self.response_json: JsonObject = response_json or {}
        self.calls: list[tuple[str, str]] = []
        self.last_post_body: JsonObject | None = None
        self.last_post_headers: dict[str, str] | None = None

    def post_capture_headers(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str],
        *,
        operation: HttpOperation,
    ) -> dict[str, str]:
        """Stand-in for :meth:`HttpClient.post_capture_headers`."""
        assert operation in {
            HttpOperation.CLAUDE_PROBE,
            HttpOperation.CLAUDE_HEARTBEAT,
        }
        self.calls.append(("POST", url))
        self.last_post_body = json_body
        self.last_post_headers = dict(headers)
        return self.response_headers

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        """Stand-in for :meth:`HttpClient.get_json`."""
        del headers
        self.calls.append(("GET", url))
        return self.response_json


def _acct(scopes: tuple[str, ...] | None) -> Account:
    """Build a setup or complete login account for its expected route.

    :param scopes: Login scopes when ``user:profile`` is present; otherwise
        select an explicit setup-token credential.
    :return: Account with sentinel fields.
    """
    credentials = (
        ClaudeLoginCredentials(
            access_token="sk-ant-oat01-test",
            refresh_token="refresh-test",
            access_expiry=KnownExpiry(datetime(2027, 1, 1, tzinfo=UTC)),
            refresh_expiry=UnknownExpiry(),
            scopes=scopes,
        )
        if scopes is not None and "user:profile" in scopes
        else ClaudeSetupTokenCredentials(access_token="sk-ant-oat01-test")
    )
    return Account(
        label=AccountLabel("t"),
        credentials=credentials,
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


# -- public header route: request shape ---------------------------
def test_fetch_via_headers_targets_messages_endpoint() -> None:
    """The probe POSTs to ``/v1/messages``, not ``/api/oauth/usage``."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    _provider().fetch_usage(_acct(()), http)
    assert http.calls == [("POST", MESSAGES_URL)]


def test_fetch_via_headers_sends_bearer_auth_and_beta() -> None:
    """Bearer auth + ``oauth-2025-04-20`` beta header are required.

    Empirically: ``x-api-key`` returns 401, ``Authorization: Bearer``
    returns 200 on ``/v1/messages`` for ``sk-ant-oat01-`` tokens.
    """
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    acct = _acct(())
    acct.credentials = replace(
        acct.credentials,
        access_token="sk-ant-oat01-secret",
    )
    _provider().fetch_usage(acct, http)
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
    _provider().fetch_usage(_acct(()), http)
    assert http.last_post_body == {
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "quota"}],
    }


# -- public header route: response conversion --------------------
def test_fetch_via_headers_parses_5h_and_7d_windows() -> None:
    """Header-path fractions are normalized to display percentages."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)
    report = _provider().fetch_usage(_acct(()), http)
    names = {w.name: w for w in report.windows}
    assert set(names) == {"5h", "7d"}
    assert round(names["5h"].utilization, 2) == _REF_5H_UTILIZATION_PERCENT
    assert round(names["7d"].utilization, 1) == _REF_7D_UTILIZATION_PERCENT
    assert names["5h"].resets_at == _REF_5H_RESET_AT


def test_fetch_via_headers_omits_window_when_headers_missing() -> None:
    """Missing 5h headers omit the 5h window — don't synthesize zeros."""
    headers = {k: v for k, v in _LIVE_HEADERS.items() if "-5h-" not in k}
    http = _FakeHttp(response_headers=headers)
    report = _provider().fetch_usage(_acct(()), http)
    assert [w.name for w in report.windows] == ["7d"]


def test_fetch_via_headers_returns_empty_windows_on_empty_response() -> None:
    """No unified headers → empty windows tuple, no crash.

    Defensive: the unified-* family is undocumented. If Anthropic
    renames or removes the headers, fetch should degrade to an
    empty report (renderer shows no bars) rather than throwing.
    """
    http = _FakeHttp(response_headers={})
    report = _provider().fetch_usage(_acct(()), http)
    assert report.windows == ()


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("anthropic-ratelimit-unified-5h-utilization", "not-a-float"),
        ("anthropic-ratelimit-unified-7d-reset", "tomorrow"),
    ],
)
def test_fetch_via_headers_rejects_malformed_window_atomically(
    header: str,
    value: str,
) -> None:
    """Malformed provider numbers become one safe typed failure."""
    http = _FakeHttp(response_headers={**_LIVE_HEADERS, header: value})

    with pytest.raises(ProviderBoundaryError) as exc_info:
        _provider().fetch_usage(_acct(()), http)

    assert exc_info.value.failure.kind is ProviderFailureKind.MALFORMED
    assert value not in repr(exc_info.value.failure)


@pytest.mark.parametrize("heartbeat", [False, True])
def test_oauth_boundaries_reject_malformed_window_safely(
    *,
    heartbeat: bool,
) -> None:
    """Usage and heartbeat never turn an invalid reset into absence."""
    raw_value = "raw-provider-reset-value"
    http = _FakeHttp(
        response_json={
            "five_hour": {
                "utilization": 0.25,
                "resets_at": raw_value,
            }
        }
    )
    account = _acct(("user:profile", "user:inference"))

    def invoke_boundary() -> object:
        if heartbeat:
            return ClaudeHeartbeat().run(account, http)
        return _provider().fetch_usage(account, http)

    with pytest.raises(ProviderBoundaryError) as exc_info:
        invoke_boundary()

    assert exc_info.value.failure.kind is ProviderFailureKind.MALFORMED
    assert raw_value not in repr(exc_info.value.failure)


# -- heartbeat/window warming -------------------------------------
def test_claude_oauth_heartbeat_skips_active_five_hour() -> None:
    """Full-scope heartbeat reads usage and skips when 5h is active."""
    http = _FakeHttp(
        response_json={
            "five_hour": {
                "utilization": 0.25,
                "resets_at": "2026-06-12T18:00:00Z",
            },
        }
    )

    result = ClaudeHeartbeat().run(
        _acct(("user:profile", "user:inference")),
        http,
    )

    assert result.status is HeartbeatStatus.ACTIVE
    assert result.warmed is False
    assert result.reset_at == _OAUTH_RESET_AT
    assert http.calls == [("GET", USAGE_URL)]


def test_claude_oauth_heartbeat_warms_inactive_five_hour() -> None:
    """Full-scope heartbeat probes messages only when 5h is inactive."""
    http = _FakeHttp(
        response_json={"five_hour": {"utilization": 0, "resets_at": None}},
        response_headers=_LIVE_HEADERS,
    )

    result = ClaudeHeartbeat().run(
        _acct(("user:profile", "user:inference")),
        http,
    )

    assert result.status is HeartbeatStatus.WARMED
    assert result.warmed is True
    assert result.reset_at == _REF_5H_RESET_AT
    assert http.calls == [("GET", USAGE_URL), ("POST", MESSAGES_URL)]
    assert http.last_post_body == {
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "quota"}],
    }


def test_claude_setup_heartbeat_uses_header_probe() -> None:
    """Setup tokens warm by sending the tiny header probe."""
    http = _FakeHttp(response_headers=_LIVE_HEADERS)

    result = ClaudeHeartbeat().run(_acct(("user:inference",)), http)

    assert result.status is HeartbeatStatus.WARMED
    assert result.warmed is True
    assert result.reset_at == _REF_5H_RESET_AT
    assert http.calls == [("POST", MESSAGES_URL)]
