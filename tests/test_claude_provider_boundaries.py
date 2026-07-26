"""Claude schema validation and setup-token process boundary tests."""

import hashlib
import os
import stat
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
import sidekick_usages.providers.claude.auth.storage.keychain
import sidekick_usages.providers.claude.provider
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialAction,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    ClaudeLoginIdentity,
    DetectedCredentials,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.profile import (
    prepare_claude_managed_profile,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
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
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    KEYCHAIN_CREDENTIAL_BYTES,
    KEYCHAIN_READ_TIMEOUT_SECONDS,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
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
    ClaudeManagedProfile,
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
    CLAUDE_LOGGED_OUT_STATUS,
    CLAUDE_LOGIN_HELP_OUTPUT,
    CLAUDE_VERSION_OUTPUT,
    ClaudeRunner,
    claude_capabilities,
    credential_payload,
    managed_profile,
    native_profile,
    profile_tree,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

SETUP_TOKEN_TIMEOUT_SECONDS = 600
_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_PRIVATE_DIRECTORY_MODE = 0o700
_PROCESS_OUTPUT_LIMIT = 1024 * 1024
_PROCESS_TIMEOUT_SECONDS = 0.01
_AUTHORITY_ID = AuthorityId("33333333-3333-4333-8333-333333333333")
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_FUTURE_EXPIRY_MS = int(_FUTURE_EXPIRY.timestamp() * 1000)
_KEYCHAIN_EXECUTABLE = Path("/usr/bin/security")
_KEYCHAIN_LOCKED_EXIT = (-25308) % 256
_KEYCHAIN_PROVENANCE = ExecutableProvenance(
    _KEYCHAIN_EXECUTABLE,
    0,
    0,
    1,
    0,
)


def _keychain_service(profile: ClaudeManagedProfile) -> str:
    digest = hashlib.sha256(str(profile.config_directory).encode()).hexdigest()
    return "Claude Code-credentials-" + digest[:8]


def _keychain_arguments(service: str) -> tuple[str, ...]:
    return (
        "find-generic-password",
        "-a",
        "sidekick-test",
        "-w",
        "-s",
        service,
    )


def _probe_runner(
    *,
    login_return_code: int = 0,
) -> ClaudeRunner:
    return ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT),
            ("auth", "status"): ClaudeCommandResult(
                1,
                CLAUDE_LOGGED_OUT_STATUS,
            ),
            ("auth", "login", "--help"): ClaudeCommandResult(
                login_return_code,
                CLAUDE_LOGIN_HELP_OUTPUT,
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
            "APPDATA": str(probe_home / "AppData" / "Roaming"),
            "CLAUDE_CONFIG_DIR": str(probe_home.parent / "config"),
            "HOME": str(probe_home),
            "LOCALAPPDATA": str(probe_home / "AppData" / "Local"),
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Protected Claude profiles use the POSIX file boundary.",
)
def test_file_profile_readback_is_exact_identity_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    profiles = profile_tree(paths)
    profile_a = managed_profile(paths, _ACCOUNT_A)
    profiles.ensure_owned_directory(profile_a.config_directory)
    payload_a = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="profile-a",
        access_expires_at=_FUTURE_EXPIRY,
    )
    profiles.write_owned_file(
        profile_a.config_directory,
        CLAUDE_CREDENTIAL_FILE,
        payload_a,
    )
    expected_identity = ClaudeLoginIdentity(
        account_id="provider-account-a",
        organization_id="provider-organization-a",
    ).provider_identity
    other_identity = ClaudeLoginIdentity(
        account_id="provider-account-b",
        organization_id="provider-organization-b",
    ).provider_identity
    reader = ClaudeManagedAuthorityReader(paths, profiles)

    snapshots = tuple(
        reader.read(
            claude_capabilities(profile_a, platform),
            REFERENCE_TIME,
            expected_identity=expected_identity,
        )
        for platform in (
            ClaudeManagedPlatform.LINUX_FILE,
            ClaudeManagedPlatform.WSL_FILE,
        )
    )

    assert all(
        snapshot.profile == profile_a
        and snapshot.provider_identity == expected_identity
        and snapshot.action is CredentialAction.NONE
        for snapshot in snapshots
    )
    metadata = managed_login_authority(
        snapshots[0],
        _AUTHORITY_ID,
        REFERENCE_TIME,
    )
    assert metadata.provider_identity == expected_identity
    assert metadata.access_expires_at == snapshots[0].access_expires_at
    assert "profile-a" not in repr(metadata)
    assert "profile-a" not in repr(snapshots[0])

    with pytest.raises(ClaudeProtectedStorageError) as mismatch:
        reader.read(
            claude_capabilities(
                profile_a,
                ClaudeManagedPlatform.LINUX_FILE,
            ),
            REFERENCE_TIME,
            expected_identity=other_identity,
        )
    assert (
        mismatch.value.code is ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
    )

    credential_path = profile_a.config_directory / CLAUDE_CREDENTIAL_FILE
    credential_path.chmod(0o644)
    with pytest.raises(ClaudeProtectedStorageError) as unsafe:
        reader.read(
            claude_capabilities(
                profile_a,
                ClaudeManagedPlatform.WSL_FILE,
            ),
            REFERENCE_TIME,
            expected_identity=expected_identity,
        )
    assert unsafe.value.code is ClaudeProtectedStorageFailure.UNSAFE

    native = native_profile(tmp_path / "native-home")
    native_path = native.config_directory / CLAUDE_CREDENTIAL_FILE
    native_path.write_bytes(payload_a)
    native_path.chmod(0o600)
    native_reader = ClaudeNativeAuthorityReader(native)
    native_capabilities = claude_capabilities(
        native,
        ClaudeManagedPlatform.LINUX_FILE,
    )
    native_snapshot = native_reader.read(
        native_capabilities,
        REFERENCE_TIME,
        expected_identity=expected_identity,
    )
    assert (
        native_snapshot.profile,
        native_snapshot.provider_identity,
    ) == (native, expected_identity)

    native_path.chmod(0o644)
    with pytest.raises(ClaudeProtectedStorageError) as native_unsafe:
        native_reader.read(
            native_capabilities,
            REFERENCE_TIME,
            expected_identity=expected_identity,
        )
    assert native_unsafe.value.code is ClaudeProtectedStorageFailure.UNSAFE


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake Keychain scenario needs the POSIX private tree.",
)
def test_keychain_readback_is_namespaced_bounded_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    profiles = profile_tree(paths)
    profile_a = managed_profile(paths, _ACCOUNT_A)
    profile_b = managed_profile(paths, _ACCOUNT_B)
    profiles.ensure_owned_directory(profile_a.config_directory)
    profiles.ensure_owned_directory(profile_b.config_directory)
    capabilities_a = claude_capabilities(
        profile_a,
        ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
    )
    capabilities_b = claude_capabilities(
        profile_b,
        ClaudeManagedPlatform.MACOS_X64_KEYCHAIN,
    )
    native = native_profile(tmp_path / "native-home")
    native_capabilities = claude_capabilities(
        native,
        ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
    )
    payload_a = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="keychain-a-secret",
        access_expires_at=_FUTURE_EXPIRY,
    )
    payload_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="keychain-b-secret",
        access_expires_at=_FUTURE_EXPIRY,
    )
    environment = {"USER": "sidekick-test"}
    service_a = _keychain_service(profile_a)
    service_b = _keychain_service(profile_b)
    native_arguments = _keychain_arguments("Claude Code-credentials")
    profile_a_arguments = _keychain_arguments(service_a)
    profile_b_arguments = _keychain_arguments(service_b)
    runner = ClaudeRunner(
        {
            native_arguments: ClaudeCommandResult(0, payload_a + b"\n"),
            profile_a_arguments: ClaudeCommandResult(0, payload_a + b"\r\n"),
            profile_b_arguments: ClaudeCommandResult(0, payload_b + b"\n"),
        }
    )
    monkeypatch.setattr(
        sidekick_usages.providers.claude.auth.storage.keychain,
        "qualify_executable",
        lambda path: _KEYCHAIN_PROVENANCE,
    )
    monkeypatch.setattr(
        sidekick_usages.providers.claude.auth.storage.keychain,
        "verify_executable",
        lambda provenance: None,
    )
    reader = ClaudeManagedAuthorityReader(paths, profiles)
    native_reader = ClaudeNativeAuthorityReader(native)

    native_snapshot = native_reader.read(
        native_capabilities,
        REFERENCE_TIME,
        environment=environment,
        runner=runner,
    )
    snapshot_a = reader.read(
        capabilities_a,
        REFERENCE_TIME,
        environment=environment,
        runner=runner,
    )
    snapshot_b = reader.read(
        capabilities_b,
        REFERENCE_TIME,
        environment=environment,
        runner=runner,
    )

    assert native_snapshot.profile == native
    assert snapshot_a.profile == profile_a
    assert snapshot_b.profile == profile_b
    assert service_a != service_b
    assert runner.calls == [
        (_KEYCHAIN_EXECUTABLE, native_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_a_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_b_arguments),
    ]
    assert runner.timeouts == [KEYCHAIN_READ_TIMEOUT_SECONDS] * 3
    assert runner.output_limits == [KEYCHAIN_CREDENTIAL_BYTES + 2] * 3
    assert (
        runner.environments
        == [{"PATH": os.defpath, "USER": "sidekick-test"}] * 3
    )
    assert "keychain-a-secret" not in repr(snapshot_a)
    assert "keychain-b-secret" not in repr(snapshot_b)

    locked_runner = ClaudeRunner(
        {
            profile_a_arguments: ClaudeCommandResult(
                _KEYCHAIN_LOCKED_EXIT,
                b"keychain-failure-secret",
            )
        }
    )
    with pytest.raises(ClaudeProtectedStorageError) as locked:
        reader.read(
            capabilities_a,
            REFERENCE_TIME,
            environment=environment,
            runner=locked_runner,
        )
    assert locked.value.code is ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED
    assert "keychain-failure-secret" not in repr(locked.value)

    profiles.write_owned_file(
        profile_a.config_directory,
        CLAUDE_CREDENTIAL_FILE,
        payload_a,
    )
    fallback_runner = ClaudeRunner(
        {
            profile_a_arguments: ClaudeCommandResult(0, payload_a),
        }
    )
    with pytest.raises(ClaudeProtectedStorageError) as fallback:
        reader.read(
            capabilities_a,
            REFERENCE_TIME,
            environment=environment,
            runner=fallback_runner,
        )
    assert (
        fallback.value.code is ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
    )
    assert fallback_runner.calls == []
