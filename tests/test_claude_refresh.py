"""Tests for Claude OAuth refresh support."""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers import claude as claude_module
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.store import Account

CLI_REFRESH_TIMEOUT_SECONDS = 60
CLI_EXPIRES_AT_MS = 1_781_270_062_459


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


def _disable_cli_refresh(monkeypatch: Any) -> None:
    """Make CLI-backed refresh unavailable for direct-HTTP tests."""
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: None)

    def _raise_not_found(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", _raise_not_found)


def test_claude_refresh_returns_false_without_refresh_token() -> None:
    """Claude refresh is skipped when nothing can be exchanged."""
    http = _FakeHttp()
    acct = _acct(refresh_token=None)

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert http.calls == []
    assert acct.access_token == "sk-ant-oat01-old"


def test_claude_refresh_uses_cli_refresh_token_login(
    monkeypatch: Any,
) -> None:
    """Claude refresh delegates to Claude Code in an isolated HOME."""
    http = _FakeHttp(response_json={"access_token": "http-unused"})
    acct = _acct()
    monkeypatch.setattr(
        claude_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def _run(
        cmd: list[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert cmd == ["/usr/bin/claude", "auth", "login", "--claudeai"]
        assert env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "refresh-old"
        assert env["CLAUDE_CODE_OAUTH_SCOPES"] == (
            "user:profile user:inference user:sessions:claude_code "
            "user:mcp_servers user:file_upload"
        )
        assert capture_output is True
        assert text is True
        assert timeout == CLI_REFRESH_TIMEOUT_SECONDS
        assert check is False
        creds_path = Path(env["HOME"]) / ".claude" / ".credentials.json"
        creds_path.parent.mkdir(parents=True)
        creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-cli",
                        "refreshToken": "sk-ant-ort01-cli",
                        "expiresAt": CLI_EXPIRES_AT_MS,
                        "subscriptionType": "team",
                        "scopes": [
                            "user:file_upload",
                            "user:inference",
                            "user:mcp_servers",
                            "user:profile",
                            "user:sessions:claude_code",
                        ],
                    }
                }
            )
        )
        return subprocess.CompletedProcess(cmd, 0, "Login successful\n", "")

    monkeypatch.setattr(subprocess, "run", _run)

    assert ClaudeProvider().refresh_token(acct, http) is True

    assert http.calls == []
    assert acct.access_token == "sk-ant-oat01-cli"
    assert acct.refresh_token == "sk-ant-ort01-cli"
    assert acct.expires_at == CLI_EXPIRES_AT_MS
    assert acct.plan == "team"
    assert acct.scopes == [
        "user:file_upload",
        "user:inference",
        "user:mcp_servers",
        "user:profile",
        "user:sessions:claude_code",
    ]


def test_claude_refresh_posts_saved_refresh_token(monkeypatch: Any) -> None:
    """Direct HTTP fallback mirrors Claude Code's refresh contract."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"access_token": "sk-ant-oat01-new"})
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is True

    assert http.calls == [
        ("POST", "https://platform.claude.com/v1/oauth/token")
    ]
    assert http.last_body == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "scope": (
            "user:profile user:inference user:sessions:claude_code "
            "user:mcp_servers user:file_upload"
        ),
        "expires_in": 31_536_000,
    }
    assert http.last_headers is not None
    assert "anthropic-beta" not in http.last_headers


def test_claude_refresh_cli_rejection_does_not_fallback_to_http(
    monkeypatch: Any,
) -> None:
    """A real Claude CLI rejection is the authoritative refresh result."""
    http = _FakeHttp(response_json={"access_token": "http-unused"})
    acct = _acct()
    monkeypatch.setattr(
        claude_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def _run(
        cmd: list[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture_output, text, timeout, check
        return subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "Login failed: Request failed with status code 400\n",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(AuthError) as exc:
        ClaudeProvider().refresh_token(acct, http)

    assert "Claude CLI refresh failed" in str(exc.value)
    assert "status code 400" in str(exc.value)
    assert http.calls == []


def test_claude_refresh_uses_saved_scopes(monkeypatch: Any) -> None:
    """Claude refresh asks for the same scopes saved with the account."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"access_token": "sk-ant-oat01-new"})
    acct = _acct()
    acct.scopes = ["user:inference", "user:profile"]

    assert ClaudeProvider().refresh_token(acct, http) is True

    assert http.last_body is not None
    assert http.last_body["scope"] == "user:inference user:profile"


def test_claude_refresh_updates_tokens_and_millisecond_expiry(
    monkeypatch: Any,
) -> None:
    """Successful refresh mutates account token metadata in-place."""
    _disable_cli_refresh(monkeypatch)
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


def test_claude_refresh_returns_false_on_auth_error(monkeypatch: Any) -> None:
    """Rejected refresh tokens leave the account untouched."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(raise_on_post=AuthError("Refresh rejected"))
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"


def test_claude_refresh_returns_false_without_access_token(
    monkeypatch: Any,
) -> None:
    """Malformed refresh responses do not partially update the account."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"refresh_token": "refresh-new"})
    acct = _acct()

    assert ClaudeProvider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"
