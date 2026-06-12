"""Tests for Claude OAuth refresh support."""

import time
from typing import Any

from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.store import Account


class _FakeHttp(HttpClient):
    """Records JSON POST calls and returns a canned response."""

    def __init__(
        self,
        response_json: dict[str, Any] | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        """:param response_json: Canned JSON response."""
        super().__init__()
        self.response_json = response_json or {}
        self.raise_on_post = raise_on_post
        self.calls: list[tuple[str, str]] = []
        self.last_body: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None

    def post_json(
        self,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Stand-in for :meth:`HttpClient.post_json`."""
        self.calls.append(("POST", url))
        self.last_body = json_body
        self.last_headers = headers or {}
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return self.response_json


def _acct(refresh_token: str | None = "refresh-old") -> Account:
    """Build a minimal Claude account for refresh tests."""
    return Account(
        label="team",
        provider_id="claude",
        access_token="sk-ant-oat01-old",
        refresh_token=refresh_token,
    )


def test_claude_refresh_returns_false_without_refresh_token() -> None:
    """Claude refresh is skipped when nothing can be exchanged."""
    http = _FakeHttp()
    acct = _acct(refresh_token=None)

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert http.calls == []
    assert acct.access_token == "sk-ant-oat01-old"


def test_claude_refresh_posts_saved_refresh_token() -> None:
    """Claude refresh uses the saved account refresh token, not local login."""
    http = _FakeHttp(response_json={"access_token": "sk-ant-oat01-new"})
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is True

    assert http.calls == [("POST", "https://api.anthropic.com/v1/oauth/token")]
    assert http.last_body == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
        "client_id": "https://claude.ai/oauth/claude-code-client-metadata",
    }
    assert http.last_headers is not None
    assert http.last_headers["anthropic-beta"] == "oauth-2025-04-20"


def test_claude_refresh_updates_tokens_and_millisecond_expiry() -> None:
    """Successful refresh mutates account token metadata in-place."""
    before_ms = int(time.time() * 1000)
    http = _FakeHttp(
        response_json={
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "refresh-new",
            "expires_in": 60,
        }
    )
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is True

    after_ms = int(time.time() * 1000)
    assert acct.access_token == "sk-ant-oat01-new"
    assert acct.refresh_token == "refresh-new"
    assert acct.expires_at is not None
    assert before_ms + 60_000 <= acct.expires_at <= after_ms + 60_000


def test_claude_refresh_returns_false_on_auth_error() -> None:
    """Rejected refresh tokens leave the account untouched."""
    http = _FakeHttp(raise_on_post=AuthError("Refresh rejected"))
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"


def test_claude_refresh_returns_false_without_access_token() -> None:
    """Malformed refresh responses do not partially update the account."""
    http = _FakeHttp(response_json={"refresh_token": "refresh-new"})
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"
