"""Claude schema validation and setup-token process boundary tests."""

import sys

import pytest

import sidekick_usages.providers.claude.provider
from sidekick_usages.core.models import DetectedCredentials
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.provider import (
    ClaudeProvider,
    ClaudeSetupToken,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.schema.usage import oauth_usage_windows
from sidekick_usages.serialization.json import JsonObject
from tests.test_claude_refresh import _FUTURE_EXPIRY_MS, _provider

SETUP_TOKEN_TIMEOUT_SECONDS = 600


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
            "refreshToken": "refresh-plan-boundary",
            "expiresAt": _FUTURE_EXPIRY_MS,
            "scopes": ["user:profile"],
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
        sidekick_usages.providers.claude.provider.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def capture(
        command: list[str],
        timeout: int,
    ) -> sidekick_usages.providers.claude.provider._CapturedSetupOutput:
        assert command == ["/usr/bin/claude", "setup-token"]
        assert timeout == SETUP_TOKEN_TIMEOUT_SECONDS
        return sidekick_usages.providers.claude.provider._CapturedSetupOutput(
            0,
            f"{raw_secret}\nToken: {first_token}\n".encode(),
        )

    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(capture),
    )

    capability: ClaudeSetupToken = _provider()
    result = capability.capture_setup_token()

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
