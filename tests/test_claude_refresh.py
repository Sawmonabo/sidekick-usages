"""Tests for Claude OAuth refresh support."""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import Account, ClaudeCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.errors import AuthError, InvalidPayloadError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.claude import provider as claude_provider_module
from sidekick_usages.providers.claude.provider import (
    SetupTokenSuccess,
    SetupTokenTimedOut,
)
from sidekick_usages.serialization import JsonObject
from tests.test_support import REFERENCE_TIME, FixedClock

CLI_REFRESH_TIMEOUT_SECONDS = 60
SETUP_TOKEN_TIMEOUT_SECONDS = 600
CLI_EXPIRES_AT_MS = 1_781_270_062_459
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _provider() -> ClaudeProvider:
    return ClaudeProvider(FixedClock())


class _FakeHttp(HttpClient):
    """Records JSON POST calls and returns a canned response."""

    def __init__(
        self,
        response_json: JsonObject | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        """:param response_json: Canned JSON response."""
        super().__init__()
        self.response_json: JsonObject = response_json or {}
        self.raise_on_post = raise_on_post
        self.calls: list[tuple[str, str]] = []
        self.last_body: JsonObject | None = None
        self.last_headers: dict[str, str] | None = None

    def post_json(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        """Stand-in for :meth:`HttpClient.post_json`."""
        self.calls.append(("POST", url))
        assert operation is HttpOperation.CLAUDE_REFRESH
        self.last_body = json_body
        self.last_headers = dict(headers or {})
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return self.response_json


def _acct(refresh_token: str | None = "refresh-old") -> Account:
    """Build a minimal Claude account for refresh tests."""
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeCredentials(
            access_token="sk-ant-oat01-old",
            refresh_token=refresh_token,
        ),
    )


def _disable_cli_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make CLI-backed refresh unavailable for direct-HTTP tests."""
    monkeypatch.setattr(
        claude_provider_module.shutil,
        "which",
        lambda name: None,
    )

    def _raise_not_found(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", _raise_not_found)


def test_claude_refresh_returns_false_without_refresh_token() -> None:
    """Claude refresh is skipped when nothing can be exchanged."""
    http = _FakeHttp()
    acct = _acct(refresh_token=None)

    assert _provider().refresh_token(acct, http) is False

    assert http.calls == []
    assert acct.access_token == "sk-ant-oat01-old"


def test_claude_refresh_uses_cli_refresh_token_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude refresh delegates to Claude Code in an isolated HOME."""
    http = _FakeHttp(response_json={"access_token": "http-unused"})
    acct = _acct()
    monkeypatch.setattr(
        claude_provider_module.shutil,
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

    assert _provider().refresh_token(acct, http) is True

    assert http.calls == []
    assert acct.access_token == "sk-ant-oat01-cli"
    assert acct.refresh_token == "sk-ant-ort01-cli"
    assert acct.expiry == KnownExpiry(
        _EPOCH + timedelta(milliseconds=CLI_EXPIRES_AT_MS)
    )
    assert acct.plan == "team"
    assert acct.scopes == (
        "user:file_upload",
        "user:inference",
        "user:mcp_servers",
        "user:profile",
        "user:sessions:claude_code",
    )


def test_claude_refresh_posts_saved_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct HTTP fallback mirrors Claude Code's refresh contract."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"access_token": "sk-ant-oat01-new"})
    acct = _acct()
    credentials = acct.credentials
    assert isinstance(credentials, ClaudeCredentials)
    acct.credentials = replace(
        credentials,
        expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=1)),
    )

    assert _provider().refresh_token(acct, http) is True

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
    assert isinstance(acct.expiry, UnknownExpiry)


def test_claude_refresh_cli_rejection_does_not_fallback_to_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Claude CLI rejection is the authoritative refresh result."""
    http = _FakeHttp(response_json={"access_token": "http-unused"})
    acct = _acct()
    monkeypatch.setattr(
        claude_provider_module.shutil,
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
        _provider().refresh_token(acct, http)

    assert "Claude CLI refresh failed" in str(exc.value)
    assert "status code 400" in str(exc.value)
    assert http.calls == []


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        ((), ""),
        (("user:inference", "user:profile"), "user:inference user:profile"),
    ],
)
def test_claude_refresh_preserves_known_scope_state(
    monkeypatch: pytest.MonkeyPatch,
    scopes: tuple[str, ...],
    expected: str,
) -> None:
    """Known-empty and populated scope sets remain distinguishable."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"access_token": "sk-ant-oat01-new"})
    acct = _acct()
    credentials = acct.credentials
    assert isinstance(credentials, ClaudeCredentials)
    acct.credentials = replace(
        credentials,
        scopes=scopes,
    )

    assert _provider().refresh_token(acct, http) is True

    assert http.last_body is not None
    assert http.last_body["scope"] == expected


def test_claude_refresh_updates_tokens_and_millisecond_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful refresh mutates account token metadata in-place."""
    _disable_cli_refresh(monkeypatch)
    clock = FixedClock()
    http = _FakeHttp(
        response_json={
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "refresh-new",
            "expires_in": 60,
        }
    )
    acct = _acct()

    assert ClaudeProvider(clock).refresh_token(acct, http) is True

    assert acct.access_token == "sk-ant-oat01-new"
    assert acct.refresh_token == "refresh-new"
    assert acct.expiry == KnownExpiry(REFERENCE_TIME + timedelta(seconds=60))
    assert clock.calls == 1


def test_claude_refresh_returns_false_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected refresh tokens leave the account untouched."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(raise_on_post=AuthError("Refresh rejected"))
    acct = _acct()

    assert _provider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"


def test_claude_refresh_returns_false_without_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed refresh responses do not partially update the account."""
    _disable_cli_refresh(monkeypatch)
    http = _FakeHttp(response_json={"refresh_token": "refresh-new"})
    acct = _acct()

    assert _provider().refresh_token(acct, http) is False

    assert acct.access_token == "sk-ant-oat01-old"
    assert acct.refresh_token == "refresh-old"


@pytest.mark.parametrize(
    "response",
    [
        {"access_token": "sk-ant-oat01-new", "refresh_token": ""},
        {"access_token": "sk-ant-oat01-new", "refresh_token": 42},
        {"access_token": "sk-ant-oat01-new", "expires_in": True},
    ],
)
def test_claude_refresh_rejects_malformed_optional_fields_atomically(
    monkeypatch: pytest.MonkeyPatch,
    response: JsonObject,
) -> None:
    """Malformed optional refresh metadata cannot rotate credentials."""
    _disable_cli_refresh(monkeypatch)
    acct = _acct()
    original = acct.credentials

    with pytest.raises(InvalidPayloadError):
        _provider().refresh_token(acct, _FakeHttp(response_json=response))

    assert acct.credentials is original


def test_setup_token_capture_filters_the_token_from_safe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture returns the first token while redacting every token line."""
    first_token = "sk-ant-oat01-synthetic-token"
    second_token = "sk-ant-oat01-other-synthetic-token"
    monkeypatch.setattr(
        claude_provider_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def _run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["/usr/bin/claude", "setup-token"]
        assert capture_output is True
        assert text is True
        assert timeout == SETUP_TOKEN_TIMEOUT_SECONDS
        assert check is False
        return subprocess.CompletedProcess(
            command,
            0,
            (
                "Opening browser\n"
                f"Token: {first_token}\n"
                f"Rotated token: {second_token}\n"
                "Complete\n"
            ),
            "",
        )

    monkeypatch.setattr(claude_provider_module.subprocess, "run", _run)

    result = _provider().capture_setup_token()

    assert result == SetupTokenSuccess(
        first_token,
        ("Opening browser", "Complete", ""),
    )


def test_setup_token_capture_reports_its_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser flow exceeding the deadline remains an explicit state."""
    monkeypatch.setattr(
        claude_provider_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def _run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(claude_provider_module.subprocess, "run", _run)

    assert isinstance(
        _provider().capture_setup_token(),
        SetupTokenTimedOut,
    )
