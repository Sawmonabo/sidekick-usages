"""Claude credential-boundary and refresh behavior tests."""

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
    RefreshSuccess,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.claude import credentials as credentials_module
from sidekick_usages.providers.claude import provider as provider_module
from sidekick_usages.providers.claude.provider import (
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)
from sidekick_usages.serialization import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    authenticated_account,
)

CLI_REFRESH_TIMEOUT_SECONDS = 60
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_FUTURE_EXPIRY_MS = int(_FUTURE_EXPIRY.timestamp() * 1000)


class _FakeHttp(HttpClient):
    """Record Claude refresh requests and return one scripted result."""

    def __init__(
        self,
        response: JsonObject | None = None,
        failure: Exception | None = None,
    ) -> None:
        super().__init__()
        self.response = response or {}
        self.failure = failure
        self.body: JsonObject | None = None

    def post_json(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        del url, headers
        assert operation is HttpOperation.CLAUDE_REFRESH
        self.body = json_body
        if self.failure is not None:
            raise self.failure
        return self.response


class _PathStageReader:
    """Read one test-produced stage after the synthetic child exits."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> bytes | None:
        """Return the staged test bytes when present."""
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None


def _provider() -> ClaudeProvider:
    return ClaudeProvider(FixedClock())


def _account(
    *,
    setup_token: bool = False,
    scopes: tuple[str, ...] = ("user:profile",),
) -> Account:
    credentials = (
        ClaudeSetupTokenCredentials(access_token="sk-ant-oat01-old")
        if setup_token
        else ClaudeLoginCredentials(
            access_token="sk-ant-oat01-old",
            refresh_token="refresh-old",
            access_expiry=KnownExpiry(_FUTURE_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=scopes,
        )
    )
    return Account(
        label=AccountLabel("claude-team"),
        credentials=credentials,
    )


def _disable_cli_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module.shutil, "which", lambda _name: None)


def _credentials(result: RefreshSuccess) -> ClaudeLoginCredentials:
    credentials = result.credentials
    assert isinstance(credentials, ClaudeLoginCredentials)
    return credentials


def test_refresh_missing_token_is_explicit_and_does_not_mutate() -> None:
    account = _account(setup_token=True)
    original = account.credentials

    result = _provider().refresh_credentials(
        authenticated_account(account),
        _FakeHttp(),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MISSING
    assert result.cause is ProviderFailureCause.MISSING_REFRESH_CREDENTIAL
    assert account.credentials is original


def test_cli_refresh_is_isolated_and_returns_complete_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account(scopes=("user:profile", "saved:scope"))
    original = account.credentials
    active_home = tmp_path / "active"
    active_home.mkdir()
    sentinel = active_home / "credentials-must-not-change"
    sentinel.write_text("active")
    monkeypatch.setattr(provider_module.platform, "system", lambda: "Linux")
    inherited = {
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:\\Windows",
        "TEMP": str(tmp_path / "temp"),
    }
    inherited_names = (
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    for name in inherited_names:
        monkeypatch.delenv(name, raising=False)
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    conflicting = {
        "ANTHROPIC_API_KEY": "test-only-anthropic-secret",
        "ANTHROPIC_AUTH_TOKEN": "test-only-auth-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "test-only-access-secret",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "test-only-refresh-secret",
        "CLAUDE_CODE_OAUTH_SCOPES": "test-only:scope",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "SIDEKICK_UNRELATED_SECRET": "test-only-unrelated-secret",
    }
    for name, value in conflicting.items():
        monkeypatch.setenv(name, value)
    for variable in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.setenv(variable, str(active_home))
    monkeypatch.setattr(
        provider_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def capture(
        command: list[str],
        timeout: int,
        *,
        env: dict[str, str],
        cwd: Path,
        umask: int,
    ) -> provider_module._CapturedSetupOutput:
        assert command == ["/usr/bin/claude", "auth", "login", "--claudeai"]
        assert env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "refresh-old"
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        assert config_dir == Path(env["HOME"]) / ".claude"
        assert cwd == Path(env["HOME"])
        assert env["USERPROFILE"] == env["HOME"]
        assert Path(env["APPDATA"]).is_relative_to(cwd)
        assert Path(env["LOCALAPPDATA"]).is_relative_to(cwd)
        assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(cwd)
        assert str(active_home) not in env.values()
        assert timeout == CLI_REFRESH_TIMEOUT_SECONDS
        assert umask == (0o077 if os.name == "posix" else -1)
        assert set(env) == {
            *inherited,
            "APPDATA",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "CLAUDE_CODE_OAUTH_SCOPES",
            "CLAUDE_CONFIG_DIR",
            "HOME",
            "LOCALAPPDATA",
            "USERPROFILE",
            "XDG_CONFIG_HOME",
        }
        assert (
            not conflicting.keys()
            - {
                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                "CLAUDE_CODE_OAUTH_SCOPES",
            }
            & env.keys()
        )
        path = config_dir / ".credentials.json"
        path.parent.mkdir(parents=True)
        oauth: JsonObject = {
            "accessToken": "sk-ant-oat01-cli",
            "refreshToken": "refresh-cli",
            "expiresAt": _FUTURE_EXPIRY_MS,
            "subscriptionType": "team",
            "scopes": ["saved:scope", "user:profile"],
        }
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": oauth,
                }
            )
        )
        return provider_module._CapturedSetupOutput(0, b"")

    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(capture),
    )

    managed_stage = tmp_path / "managed-refresh-stage"
    managed_stage.mkdir(mode=0o700)
    result = _provider().refresh_credentials_in_stage(
        authenticated_account(account),
        _FakeHttp(),
        managed_stage,
        _PathStageReader(managed_stage / ".claude" / ".credentials.json"),
    )

    assert isinstance(result, RefreshSuccess)
    refreshed = _credentials(result)
    assert refreshed.access_token == "sk-ant-oat01-cli"
    assert refreshed.refresh_token == "refresh-cli"
    assert refreshed.access_expiry == KnownExpiry(_FUTURE_EXPIRY)
    assert refreshed.scopes == ("saved:scope", "user:profile")
    assert result.plan == "team"
    assert account.credentials is original
    assert sentinel.read_text() == "active"
    assert not (tmp_path / "sidekick-claude-refresh-unmanaged").exists()


def test_macos_refresh_uses_http_without_invoking_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module.platform, "system", lambda: "Darwin")

    def unexpected_cli_lookup(_name: str) -> str | None:
        pytest.fail("macOS saved-account refresh must not invoke Claude CLI")

    monkeypatch.setattr(provider_module.shutil, "which", unexpected_cli_lookup)
    http = _FakeHttp(
        {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "refresh-new",
            "expires_in": 60,
        }
    )

    result = _provider().refresh_credentials(
        authenticated_account(_account()),
        http,
    )

    assert isinstance(result, RefreshSuccess)
    assert _credentials(result).access_token == "sk-ant-oat01-new"
    assert http.body is not None


@pytest.mark.parametrize(
    ("scopes", "expected_scope"),
    [
        (("user:profile",), "user:profile"),
        (
            ("user:inference", "user:profile"),
            "user:inference user:profile",
        ),
    ],
)
def test_http_refresh_preserves_scope_state_and_returns_new_credentials(
    monkeypatch: pytest.MonkeyPatch,
    scopes: tuple[str, ...],
    expected_scope: str,
) -> None:
    _disable_cli_refresh(monkeypatch)
    account = _account(scopes=scopes)
    original = account.credentials
    http = _FakeHttp(
        {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "refresh-new",
            "expires_in": 60,
        }
    )

    result = _provider().refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, RefreshSuccess)
    refreshed = _credentials(result)
    assert refreshed.access_token == "sk-ant-oat01-new"
    assert refreshed.refresh_token == "refresh-new"
    assert refreshed.access_expiry == KnownExpiry(
        REFERENCE_TIME + timedelta(seconds=60)
    )
    assert refreshed.scopes == scopes
    assert http.body is not None
    assert http.body["scope"] == expected_scope
    assert account.credentials is original


def test_refresh_rejection_is_typed_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_cli_refresh(monkeypatch)
    account = _account()
    original = account.credentials
    raw_secret = "sk-ant-oat01-rejected-secret"

    result = _provider().refresh_credentials(
        authenticated_account(account),
        _FakeHttp(failure=AuthError(raw_secret)),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert result.cause is ProviderFailureCause.PROVIDER_REJECTED_REFRESH
    assert result.message == "Claude rejected the saved subscription login."
    assert "log in again" not in result.message.lower()
    assert raw_secret not in repr(result)
    assert account.credentials is original


def test_transient_refresh_failure_is_a_cause_without_recovery_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_cli_refresh(monkeypatch)

    result = _provider().refresh_credentials(
        authenticated_account(_account()),
        _FakeHttp(failure=TransientError("raw provider detail")),
    )

    assert isinstance(result, ProviderFailure)
    assert result.cause is (
        ProviderFailureCause.REFRESH_TEMPORARILY_UNAVAILABLE
    )
    assert result.message == "Claude refresh is temporarily unavailable."
    assert "raw provider detail" not in repr(result)
    assert "log in again" not in result.message.lower()


def test_cli_rejection_is_authoritative_and_does_not_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        provider_module.shutil,
        "which",
        lambda _name: "/usr/bin/claude",
    )

    def capture(
        command: list[str],
        timeout: int,
        **_kwargs: object,
    ) -> provider_module._CapturedSetupOutput:
        assert timeout == CLI_REFRESH_TIMEOUT_SECONDS
        return provider_module._CapturedSetupOutput(
            1,
            b"rejected sk-ant-oat01-raw-secret",
        )

    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(capture),
    )
    account = _account()
    original = account.credentials
    http = _FakeHttp({"access_token": "sk-ant-oat01-http-unused"})

    stage_home = tmp_path / "managed-refresh-stage"
    stage_home.mkdir(mode=0o700)
    result = _provider().refresh_credentials_in_stage(
        authenticated_account(account),
        http,
        stage_home,
        _PathStageReader(stage_home / ".claude" / ".credentials.json"),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert result.cause is ProviderFailureCause.PROVIDER_REJECTED_REFRESH
    assert result.message == "Claude rejected the saved subscription login."
    assert "log in again" not in result.message.lower()
    assert "raw-secret" not in repr(result)
    assert http.body is None
    assert account.credentials is original


@pytest.mark.parametrize(
    ("capture", "expected_cause"),
    [
        (
            SetupTokenTimedOut(),
            ProviderFailureCause.REFRESH_TIMED_OUT,
        ),
        (
            SetupTokenUnreadable(),
            ProviderFailureCause.REFRESH_PROCESS_UNAVAILABLE,
        ),
    ],
)
def test_cli_refresh_reuses_bounded_process_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: SetupTokenTimedOut | SetupTokenUnreadable,
    expected_cause: ProviderFailureCause,
) -> None:
    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(lambda *_args, **_kwargs: capture),
    )

    result = ClaudeProvider._run_cli_refresh(
        "/usr/bin/claude",
        {},
        tmp_path,
    )

    assert isinstance(result, ProviderFailure)
    assert result.cause is expected_cause


def test_cli_refresh_identity_mismatch_has_cause_only_state() -> None:
    previous = ClaudeLoginCredentials(
        access_token="sk-ant-oat01-old",
        refresh_token="refresh-old",
        access_expiry=KnownExpiry(_FUTURE_EXPIRY),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
        identity=ClaudeLoginIdentity(
            account_id="account-old",
            organization_id="organization-old",
        ),
    )
    detected = DetectedCredentials(
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-new",
            refresh_token="refresh-new",
            access_expiry=KnownExpiry(_FUTURE_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
            identity=ClaudeLoginIdentity(
                account_id="account-new",
                organization_id="organization-new",
            ),
        )
    )

    with pytest.raises(ProviderBoundaryError) as exc_info:
        ClaudeProvider._cli_refresh_success(previous, detected)

    assert exc_info.value.failure.cause is (
        ProviderFailureCause.REFRESHED_IDENTITY_MISMATCH
    )
    assert "log in again" not in exc_info.value.failure.message.lower()


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ({"refresh_token": "refresh-new"}, ProviderFailureKind.INCOMPLETE),
        (
            {
                "access_token": "sk-ant-oat01-new",
                "refresh_token": "",
                "expires_in": 60,
            },
            ProviderFailureKind.MALFORMED,
        ),
        (
            {
                "access_token": "sk-ant-oat01-new",
                "expires_in": True,
            },
            ProviderFailureKind.MALFORMED,
        ),
    ],
)
def test_malformed_refresh_is_atomic_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    response: JsonObject,
    kind: ProviderFailureKind,
) -> None:
    _disable_cli_refresh(monkeypatch)
    account = _account()
    original = account.credentials
    raw_identity = "long.account.name@example.test"
    response["provider_identity"] = raw_identity

    with pytest.raises(ProviderBoundaryError) as exc_info:
        _provider().refresh_credentials(
            authenticated_account(account),
            _FakeHttp(response),
        )

    assert exc_info.value.failure.kind is kind
    assert exc_info.value.failure.cause is (
        ProviderFailureCause.REFRESH_OUTPUT_INCOMPLETE
        if kind is ProviderFailureKind.INCOMPLETE
        else ProviderFailureCause.REFRESH_OUTPUT_MALFORMED
    )
    rendered = repr(exc_info.value.failure)
    assert raw_identity not in rendered
    assert "sk-ant-oat01-new" not in rendered
    assert account.credentials is original


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    [
        (None, ProviderFailureKind.MISSING),
        (b"{", ProviderFailureKind.MALFORMED),
        (
            b'{"claudeAiOauth":{"refreshToken":"refresh-only"}}',
            ProviderFailureKind.INCOMPLETE,
        ),
        (
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-expired",
                        "refreshToken": "refresh-expired",
                        "expiresAt": int(
                            (REFERENCE_TIME - timedelta(seconds=1)).timestamp()
                            * 1000
                        ),
                        "scopes": ["user:profile"],
                    }
                }
            ).encode(),
            ProviderFailureKind.EXPIRED,
        ),
        (
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-detected",
                        "refreshToken": "refresh-detected",
                        "expiresAt": _FUTURE_EXPIRY_MS,
                        "scopes": ["user:inference", "user:profile"],
                    }
                }
            ).encode(),
            None,
        ),
    ],
)
def test_detection_distinguishes_credential_source_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
    expected_kind: ProviderFailureKind | None,
) -> None:
    monkeypatch.setattr(credentials_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(credentials_module.Path, "home", lambda: tmp_path)
    path = tmp_path / ".claude" / ".credentials.json"
    if payload is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)

    result = _provider().detect_credentials()

    if expected_kind is None:
        assert isinstance(result, DetectedCredentials)
        assert result.access_token == "sk-ant-oat01-detected"
        assert result.scopes == ("user:inference", "user:profile")
        return
    assert isinstance(result, ProviderFailure)
    assert result.kind is expected_kind
    if expected_kind is ProviderFailureKind.EXPIRED:
        assert result.cause is (ProviderFailureCause.ACCESS_CREDENTIAL_EXPIRED)


def test_expired_login_credential_fails_before_provider_contact() -> None:
    account = Account(
        label=AccountLabel("expired-login"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-current",
            refresh_token="refresh-expired",
            access_expiry=KnownExpiry(_FUTURE_EXPIRY),
            refresh_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
            scopes=("user:profile",),
        ),
    )
    http = _FakeHttp({"access_token": "sk-ant-oat01-unused"})

    result = _provider().refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, ProviderFailure)
    assert result.cause is ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED
    assert result.message == "The saved Claude login credential has expired."
    assert http.body is None


def test_detection_reports_unreadable_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(credentials_module.Path, "home", lambda: tmp_path)
    path = tmp_path / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    def unreadable(_path: Path) -> bytes:
        raise PermissionError

    monkeypatch.setattr(credentials_module, "_read_bounded", unreadable)

    result = _provider().detect_credentials()

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.UNREADABLE


@pytest.mark.parametrize(
    ("primary_state", "expected_kind"),
    [
        ("malformed", ProviderFailureKind.MALFORMED),
        ("unreadable", ProviderFailureKind.UNREADABLE),
        ("expired", ProviderFailureKind.EXPIRED),
    ],
)
def test_detection_never_bypasses_a_nonmissing_primary_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_state: str,
    expected_kind: ProviderFailureKind,
) -> None:
    monkeypatch.setattr(credentials_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(credentials_module.Path, "home", lambda: tmp_path)
    primary = tmp_path / ".claude" / ".credentials.json"
    fallback = tmp_path / ".config" / "claude" / ".credentials.json"
    primary.parent.mkdir(parents=True)
    if primary_state == "malformed":
        primary.write_text("{")
    else:
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-primary",
                        "refreshToken": "refresh-primary",
                        "expiresAt": (
                            _FUTURE_EXPIRY_MS
                            if primary_state == "unreadable"
                            else 1
                        ),
                        "scopes": ["user:profile"],
                    }
                }
            )
        )
    fallback.parent.mkdir(parents=True)
    fallback.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-fallback",
                    "refreshToken": "refresh-fallback",
                    "expiresAt": _FUTURE_EXPIRY_MS,
                    "scopes": ["user:profile"],
                }
            }
        )
    )
    if primary_state == "unreadable":
        read_bounded = credentials_module._read_bounded

        def read_with_primary_failure(path: Path) -> bytes:
            if path == primary:
                raise PermissionError
            return read_bounded(path)

        monkeypatch.setattr(
            credentials_module,
            "_read_bounded",
            read_with_primary_failure,
        )

    result = _provider().detect_credentials()

    assert isinstance(result, ProviderFailure)
    assert result.kind is expected_kind


@pytest.mark.parametrize(
    ("return_code", "expected"),
    [
        ((-25300) % 256, ProviderFailureKind.MISSING),
        (1, ProviderFailureKind.UNREADABLE),
    ],
)
def test_macos_keychain_distinguishes_absence_from_access_failure(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    expected: ProviderFailureKind,
) -> None:
    monkeypatch.setattr(
        credentials_module.platform,
        "system",
        lambda: "Darwin",
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(return_code, ["security"])

    monkeypatch.setattr(credentials_module.subprocess, "run", fail)

    result = _provider().detect_credentials()

    assert isinstance(result, ProviderFailure)
    assert result.kind is expected
