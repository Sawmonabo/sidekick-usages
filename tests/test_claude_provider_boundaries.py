"""Claude schema validation and setup-token process boundary tests."""

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
import sidekick_usages.providers.claude.provider
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import DetectedCredentials
from sidekick_usages.credentials.claude.managed.profile import (
    prepare_claude_managed_profile,
)
from sidekick_usages.paths import managed_claude_config_dir
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    SUPPORTED_CLAUDE_VERSION,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    SetupTokenSuccess,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.schema.usage import oauth_usage_windows
from sidekick_usages.providers.claude.types import (
    ClaudeProcessFailure,
    ClaudeSetupToken,
)
from sidekick_usages.serialization.json import JsonObject
from tests.test_claude_refresh import _FUTURE_EXPIRY_MS, _provider
from tests.test_support import make_application_paths

SETUP_TOKEN_TIMEOUT_SECONDS = 600
_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_CLAUDE_VERSION_OUTPUT = b"2.1.220 (Claude Code)\n"
_LOGIN_HELP_OUTPUT = (
    b"Usage: claude auth login "
    b"[--claudeai] [--console] [--email <email>] [--sso]\n"
)
_PRIVATE_DIRECTORY_MODE = 0o700
_STATUS_OUTPUT = (
    b'{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}\n'
)
_PROCESS_OUTPUT_LIMIT = 1024 * 1024
_PROCESS_TIMEOUT_SECONDS = 0.01


class _ClaudeRunner:
    """Return configured results for exact read-only Claude commands."""

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], ClaudeCommandResult],
    ) -> None:
        self._responses = responses
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.environments: list[dict[str, str] | None] = []
        self.working_directories: list[Path | None] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
    ) -> ClaudeCommandResult:
        del (
            timeout_seconds,
            maximum_output_bytes,
            umask,
        )
        arguments = argv[1:]
        self.calls.append((Path(argv[0]), arguments))
        self.environments.append(
            None if environment is None else dict(environment)
        )
        self.working_directories.append(working_directory)
        try:
            return self._responses[arguments]
        except KeyError:
            raise AssertionError(
                f"Unexpected Claude command: {arguments!r}"
            ) from None


def _probe_runner(
    *,
    login_return_code: int = 0,
) -> _ClaudeRunner:
    return _ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, _CLAUDE_VERSION_OUTPUT),
            ("auth", "status"): ClaudeCommandResult(1, _STATUS_OUTPUT),
            ("auth", "login", "--help"): ClaudeCommandResult(
                login_return_code,
                _LOGIN_HELP_OUTPUT,
            ),
        }
    )


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
    runner = _ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, _CLAUDE_VERSION_OUTPUT),
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

    capability: ClaudeSetupToken = _provider()
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

    assert failure.value.code is ClaudeProcessFailure.OUTPUT_UNREADABLE


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Protected managed Claude profiles are POSIX-only in Task 1.",
)
def test_supported_claude_boundary_freezes_executable_and_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    profiles = PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
    executable_path = Path(sys.executable).resolve()
    which_calls: list[str] = []
    runner = _probe_runner()
    source_environment = {
        "ANTHROPIC_API_KEY": "synthetic-native-api-key",
        "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-native-oauth",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "native-config"),
        "PATH": os.environ["PATH"],
    }

    def resolve(command: str, path: str | None = None) -> str:
        del path
        which_calls.append(command)
        return str(executable_path)

    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        resolve,
    )

    capabilities = prepare_claude_managed_profile(
        paths,
        profiles,
        _ACCOUNT_A,
        environment=source_environment,
        host=HostPlatform.LINUX,
        runner=runner,
    )
    profile_a = capabilities.profile.config_directory

    assert which_calls == ["claude"]
    assert capabilities.executable.provenance == (
        ExecutableProvenance.from_stat(
            executable_path,
            executable_path.stat(),
        )
    )
    assert capabilities.executable.version == SUPPORTED_CLAUDE_VERSION
    assert runner.calls == [
        (executable_path, ("--version",)),
        (executable_path, ("auth", "status")),
        (executable_path, ("auth", "login", "--help")),
    ]
    for environment, working_directory in zip(
        runner.environments,
        runner.working_directories,
        strict=True,
    ):
        assert environment is not None
        probe_home = Path(environment["HOME"])
        assert environment == {
            "CLAUDE_CONFIG_DIR": str(probe_home.parent / "config"),
            "HOME": str(probe_home),
            "PATH": source_environment["PATH"],
            "USERPROFILE": str(probe_home),
            "XDG_CONFIG_HOME": str(probe_home / ".config"),
        }
        assert working_directory == probe_home
    assert capabilities.platform is ClaudeManagedPlatform.LINUX_FILE
    assert profile_a == (paths.private_claude_profiles / str(_ACCOUNT_A))
    assert managed_claude_config_dir(paths, _ACCOUNT_A) == profile_a
    assert managed_claude_config_dir(paths, _ACCOUNT_B) != profile_a
    assert stat.S_IMODE(profile_a.stat().st_mode) == _PRIVATE_DIRECTORY_MODE


@pytest.mark.parametrize(
    (
        "escaped_root",
        "login_return_code",
        "host",
        "expected_failure",
        "expected_arguments",
    ),
    [
        (
            True,
            0,
            HostPlatform.LINUX,
            ClaudeManagedFailure.PROFILE_UNSAFE,
            (),
        ),
        (
            False,
            2,
            HostPlatform.LINUX,
            ClaudeManagedFailure.LOGIN_UNSUPPORTED,
            (
                ("--version",),
                ("auth", "status"),
                ("auth", "login", "--help"),
            ),
        ),
        (
            False,
            0,
            HostPlatform.WINDOWS,
            ClaudeManagedFailure.FEATURE_DISABLED,
            (),
        ),
    ],
    ids=("profile-escape", "login-capability", "native-windows"),
)
def test_claude_boundary_rejects_distinct_preflight_gates(
    escaped_root: bool,
    login_return_code: int,
    host: HostPlatform,
    expected_failure: ClaudeManagedFailure,
    expected_arguments: tuple[tuple[str, ...], ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    if escaped_root:
        paths = replace(
            paths,
            private_claude_profiles=(
                paths.private_claude_profiles / ".." / ".." / "escape"
            ),
        )
    profiles = PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
    which_calls: list[str] = []
    runner = _probe_runner(login_return_code=login_return_code)

    def resolve(command: str, path: str | None = None) -> str:
        del path
        which_calls.append(command)
        return sys.executable

    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        resolve,
    )

    with pytest.raises(ClaudeManagedError) as failure:
        prepare_claude_managed_profile(
            paths,
            profiles,
            _ACCOUNT_A,
            environment={"PATH": os.environ["PATH"]},
            host=host,
            runner=runner,
        )

    actual_arguments = tuple(arguments for _path, arguments in runner.calls)
    assert failure.value.code is expected_failure
    assert actual_arguments == expected_arguments
    assert which_calls == (["claude"] if expected_arguments else [])
    assert not paths.private_claude_profiles.exists()
