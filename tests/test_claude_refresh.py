"""Claude credential-boundary and refresh behavior tests."""

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
    RefreshSuccess,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.claude import credentials as credentials_module
from sidekick_usages.providers.claude import provider as provider_module
from sidekick_usages.providers.claude.provider import (
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)
from sidekick_usages.providers.claude.schemas import (
    oauth_usage_windows,
    parse_credentials_blob,
)
from sidekick_usages.serialization import JsonObject
from tests.test_support import REFERENCE_TIME, FixedClock

CLI_REFRESH_TIMEOUT_SECONDS = 60
SETUP_TOKEN_TIMEOUT_SECONDS = 600
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


def _provider() -> ClaudeProvider:
    return ClaudeProvider(FixedClock())


def _account(
    *,
    refresh_token: str | None = "refresh-old",
    scopes: tuple[str, ...] | None = None,
) -> Account:
    return Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeCredentials(
            access_token="sk-ant-oat01-old",
            refresh_token=refresh_token,
            scopes=scopes,
        ),
    )


def _disable_cli_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module.shutil, "which", lambda _name: None)


def _credentials(result: RefreshSuccess) -> ClaudeCredentials:
    credentials = result.credentials
    assert isinstance(credentials, ClaudeCredentials)
    return credentials


def test_refresh_missing_token_is_explicit_and_does_not_mutate() -> None:
    account = _account(refresh_token=None)
    original = account.credentials

    result = _provider().refresh_credentials(account, _FakeHttp())

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MISSING
    assert account.credentials is original


@pytest.mark.parametrize(
    ("include_refresh_token", "expected_refresh_token"),
    [(True, "refresh-cli"), (False, "refresh-old")],
)
def test_cli_refresh_is_isolated_and_returns_complete_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_refresh_token: bool,
    expected_refresh_token: str,
) -> None:
    account = _account(scopes=("saved:scope",))
    original = account.credentials
    active_home = tmp_path / "active"
    active_home.mkdir()
    sentinel = active_home / "credentials-must-not-change"
    sentinel.write_text("active")
    monkeypatch.setattr(provider_module.platform, "system", lambda: "Linux")
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

    def run(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        stdout: int,
        stderr: int,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
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
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.DEVNULL
        assert check is False
        assert timeout == CLI_REFRESH_TIMEOUT_SECONDS
        path = config_dir / ".credentials.json"
        path.parent.mkdir(parents=True)
        oauth: JsonObject = {
            "accessToken": "sk-ant-oat01-cli",
            "expiresAt": _FUTURE_EXPIRY_MS,
            "subscriptionType": "team",
            "scopes": [],
        }
        if include_refresh_token:
            oauth["refreshToken"] = "refresh-cli"
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": oauth,
                }
            )
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(provider_module.subprocess, "run", run)

    result = _provider().refresh_credentials(account, _FakeHttp())

    assert isinstance(result, RefreshSuccess)
    refreshed = _credentials(result)
    assert refreshed.access_token == "sk-ant-oat01-cli"
    assert refreshed.refresh_token == expected_refresh_token
    assert refreshed.expiry == KnownExpiry(_FUTURE_EXPIRY)
    assert refreshed.scopes == ()
    assert result.plan == "team"
    assert account.credentials is original
    assert sentinel.read_text() == "active"


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
        }
    )

    result = _provider().refresh_credentials(_account(), http)

    assert isinstance(result, RefreshSuccess)
    assert _credentials(result).access_token == "sk-ant-oat01-new"
    assert http.body is not None


@pytest.mark.parametrize(
    ("scopes", "expected_scope"),
    [
        ((), ""),
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

    result = _provider().refresh_credentials(account, http)

    assert isinstance(result, RefreshSuccess)
    refreshed = _credentials(result)
    assert refreshed.access_token == "sk-ant-oat01-new"
    assert refreshed.refresh_token == "refresh-new"
    assert refreshed.expiry == KnownExpiry(
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
        account,
        _FakeHttp(failure=AuthError(raw_secret)),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert raw_secret not in repr(result)
    assert account.credentials is original


def test_cli_rejection_is_authoritative_and_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        provider_module.shutil,
        "which",
        lambda _name: "/usr/bin/claude",
    )

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "rejected sk-ant-oat01-raw-secret",
        )

    monkeypatch.setattr(provider_module.subprocess, "run", run)
    account = _account()
    original = account.credentials
    http = _FakeHttp({"access_token": "sk-ant-oat01-http-unused"})

    result = _provider().refresh_credentials(account, http)

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert "raw-secret" not in repr(result)
    assert http.body is None
    assert account.credentials is original


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ({"refresh_token": "refresh-new"}, ProviderFailureKind.INCOMPLETE),
        (
            {
                "access_token": "sk-ant-oat01-new",
                "refresh_token": "",
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
        _provider().refresh_credentials(account, _FakeHttp(response))

    assert exc_info.value.failure.kind is kind
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
                        "expiresAt": int(
                            (REFERENCE_TIME - timedelta(seconds=1)).timestamp()
                            * 1000
                        ),
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
                        "expiresAt": (
                            _FUTURE_EXPIRY_MS
                            if primary_state == "unreadable"
                            else 1
                        ),
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
                    "expiresAt": _FUTURE_EXPIRY_MS,
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


def test_credential_validation_aggregates_only_safe_paths() -> None:
    raw_identity = "long.account.name@example.test"

    with pytest.raises(ProviderBoundaryError) as exc_info:
        parse_credentials_blob(
            {
                "claudeAiOauth": {
                    "accessToken": 42,
                    "scopes": ["user:profile", 7],
                    "identity": raw_identity,
                }
            }
        )

    rendered = str(exc_info.value)
    assert "claudeAiOauth.accessToken" in rendered
    assert "claudeAiOauth.scopes.1" in rendered
    assert raw_identity not in rendered


@pytest.mark.parametrize(
    ("plan", "expected_kind"),
    [("é" * 128, None), ("é" * 129, ProviderFailureKind.MALFORMED)],
)
def test_subscription_plan_uses_its_utf8_byte_limit(
    plan: str,
    expected_kind: ProviderFailureKind | None,
) -> None:
    blob: JsonObject = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-plan-boundary",
            "subscriptionType": plan,
        }
    }

    if expected_kind is None:
        assert parse_credentials_blob(blob).plan == plan
        return
    with pytest.raises(ProviderBoundaryError) as exc_info:
        parse_credentials_blob(blob)
    assert exc_info.value.failure.kind is expected_kind


def test_huge_usage_integer_becomes_a_safe_boundary_failure() -> None:
    with pytest.raises(ProviderBoundaryError) as exc_info:
        oauth_usage_windows(
            {
                "five_hour": {
                    "utilization": 10**400,
                    "resets_at": None,
                }
            }
        )

    assert exc_info.value.failure.kind is ProviderFailureKind.MALFORMED
    assert exc_info.value.failure.fields == ("five_hour.utilization",)


def test_manual_token_normalization_is_provider_owned_and_safe() -> None:
    provider = _provider()

    valid = provider.credentials_from_token("sk-ant-oat01-manual")
    invalid = provider.credentials_from_token("raw-secret-invalid")

    assert isinstance(valid, DetectedCredentials)
    assert valid.access_token == "sk-ant-oat01-manual"
    assert isinstance(invalid, ProviderFailure)
    assert invalid.kind is ProviderFailureKind.MALFORMED
    assert "raw-secret-invalid" not in repr(invalid)


def test_setup_token_capture_returns_no_arbitrary_process_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_token = "sk-ant-oat01-synthetic-token"
    raw_secret = "oauth-code=arbitrary-secret-sentinel"
    monkeypatch.setattr(
        provider_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def capture(
        command: list[str],
        timeout: int,
    ) -> provider_module._CapturedSetupOutput:
        assert command == ["/usr/bin/claude", "setup-token"]
        assert timeout == SETUP_TOKEN_TIMEOUT_SECONDS
        return provider_module._CapturedSetupOutput(
            0,
            f"{raw_secret}\nToken: {first_token}\n".encode(),
        )

    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(capture),
    )

    result = _provider().capture_setup_token()

    assert result == SetupTokenSuccess(first_token)
    assert first_token not in repr(result)
    assert raw_secret not in repr(result)
    assert not hasattr(result, "output_lines")


def test_setup_token_process_timeout_is_explicit() -> None:
    result = ClaudeProvider._capture_setup_output(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(1)",
        ],
        0,
    )

    assert isinstance(result, SetupTokenTimedOut)


def test_setup_token_process_output_is_bounded() -> None:
    result = ClaudeProvider._capture_setup_output(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 1048577)",
        ],
        SETUP_TOKEN_TIMEOUT_SECONDS,
    )

    assert isinstance(result, SetupTokenUnreadable)
