"""Claude schema validation and setup-token process boundary tests."""

import sys
from datetime import timedelta

import pytest

import sidekick_usages.platform.executable
import sidekick_usages.providers.claude.provider
from sidekick_usages.core.models import DetectedCredentials
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    SetupTokenSuccess,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.schema.usage import oauth_usage_windows
from sidekick_usages.providers.claude.types import (
    ClaudeProcessFailure,
    ClaudeSetupToken,
)
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.claude.managed import (
    CLAUDE_VERSION_OUTPUT,
    ClaudeRunner,
)
from tests.support.time import REFERENCE_TIME, FixedClock

SETUP_TOKEN_TIMEOUT_SECONDS = 600
_PROCESS_OUTPUT_LIMIT = 1024 * 1024
_PROCESS_TIMEOUT_SECONDS = 0.01
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_FUTURE_EXPIRY_MS = int(_FUTURE_EXPIRY.timestamp() * 1000)


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
    provider = ClaudeProvider(FixedClock())

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
    runner = ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT),
            ("setup-token",): ClaudeCommandResult(
                0,
                f"{raw_secret}\nToken: {first_token}\n".encode(),
            ),
        }
    )
    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )
    monkeypatch.setattr(
        sidekick_usages.providers.claude.provider,
        "run_bounded_claude_command",
        runner,
    )

    capability: ClaudeSetupToken = ClaudeProvider(FixedClock())
    result = capability.capture_setup_token()

    assert result == SetupTokenSuccess(first_token)
    assert first_token not in repr(result)
    assert raw_secret not in repr(result)
    assert not hasattr(result, "output_lines")
    assert tuple(arguments for _path, arguments in runner.calls) == (
        ("--version",),
        ("setup-token",),
    )


def test_setup_token_process_timeout_is_explicit() -> None:
    with pytest.raises(ClaudeProcessError) as failure:
        run_bounded_claude_command(
            (
                sys.executable,
                "-c",
                "import time; time.sleep(1)",
            ),
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
            maximum_output_bytes=_PROCESS_OUTPUT_LIMIT,
        )

    assert failure.value.code is ClaudeProcessFailure.TIMED_OUT

    cancellation_checks = 0

    def cancelled() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks > 1

    with pytest.raises(ClaudeProcessError) as cancellation:
        run_bounded_claude_command(
            (
                sys.executable,
                "-c",
                "import time; time.sleep(1)",
            ),
            timeout_seconds=SETUP_TOKEN_TIMEOUT_SECONDS,
            maximum_output_bytes=_PROCESS_OUTPUT_LIMIT,
            cancelled=cancelled,
        )

    assert cancellation.value.code is ClaudeProcessFailure.CANCELLED
    assert cancellation_checks > 1


def test_setup_token_process_output_is_bounded() -> None:
    with pytest.raises(ClaudeProcessError) as failure:
        run_bounded_claude_command(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 1048577)",
            ),
            timeout_seconds=SETUP_TOKEN_TIMEOUT_SECONDS,
            maximum_output_bytes=_PROCESS_OUTPUT_LIMIT,
        )

    assert failure.value.code is ClaudeProcessFailure.OUTPUT_TOO_LARGE
