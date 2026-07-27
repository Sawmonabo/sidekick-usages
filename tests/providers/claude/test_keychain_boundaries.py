"""Claude Keychain protected-profile boundary tests."""

import hashlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import sidekick_usages.providers.claude.auth.storage.keychain
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    CLAUDE_CREDENTIAL_BYTES,
    KEYCHAIN_READ_TIMEOUT_SECONDS,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeManagedProfile,
)
from tests.fakes.claude.managed import (
    ClaudeRunner,
    claude_capabilities,
    claude_profile_status_responses,
    credential_payload,
    managed_profile,
    native_profile,
    profile_tree,
)
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME

_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_KEYCHAIN_ACCESS_DENIED_EXIT = (-25293) % 256
_KEYCHAIN_EXECUTABLE = Path("/usr/bin/security")
_KEYCHAIN_LOCKED_EXIT = (-25308) % 256
_KEYCHAIN_MISSING_EXIT = (-25300) % 256
_KEYCHAIN_PROVENANCE = ExecutableProvenance(
    _KEYCHAIN_EXECUTABLE.absolute(),
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


def _prove_keychain_failure_contract(
    reader: ClaudeManagedAuthorityReader,
    capabilities: ClaudeCapabilities,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    profiles: PrivateCredentialTree,
    payload: bytes,
) -> None:
    """Prove bounded Keychain failures and both plaintext checks."""
    for return_code, expected_failure in (
        (
            _KEYCHAIN_LOCKED_EXIT,
            ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED,
        ),
        (
            _KEYCHAIN_ACCESS_DENIED_EXIT,
            ClaudeProtectedStorageFailure.KEYCHAIN_ACCESS_DENIED,
        ),
        (
            _KEYCHAIN_MISSING_EXIT,
            ClaudeProtectedStorageFailure.MISSING,
        ),
    ):
        rejected_runner = ClaudeRunner(
            {
                arguments: ClaudeCommandResult(
                    return_code,
                    b"keychain-failure-secret",
                )
            }
        )
        with pytest.raises(ClaudeProtectedStorageError) as rejected:
            reader.read(
                capabilities,
                REFERENCE_TIME,
                environment=environment,
                runner=rejected_runner,
            )
        assert rejected.value.code is expected_failure
        assert "keychain-failure-secret" not in repr(rejected.value)

    oversized_runner = ClaudeRunner(
        {
            arguments: ClaudeCommandResult(
                0,
                b"x" * (CLAUDE_CREDENTIAL_BYTES + 1),
            )
        }
    )
    with pytest.raises(ClaudeProtectedStorageError) as oversized:
        reader.read(
            capabilities,
            REFERENCE_TIME,
            environment=environment,
            runner=oversized_runner,
        )
    assert oversized.value.code is ClaudeProtectedStorageFailure.MALFORMED

    def create_plaintext_after_read(
        actual: tuple[str, ...],
        process_environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        del process_environment, working_directory
        assert actual == arguments
        profiles.write_owned_file(
            capabilities.profile.config_directory,
            CLAUDE_CREDENTIAL_FILE,
            payload,
        )
        return ClaudeCommandResult(0, payload)

    after_runner = ClaudeRunner(script=create_plaintext_after_read)
    with pytest.raises(ClaudeProtectedStorageError) as after_fallback:
        reader.read(
            capabilities,
            REFERENCE_TIME,
            environment=environment,
            runner=after_runner,
        )
    assert after_fallback.value.code is (
        ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
    )
    assert len(after_runner.calls) == 1

    before_runner = ClaudeRunner({arguments: ClaudeCommandResult(0, payload)})
    with pytest.raises(ClaudeProtectedStorageError) as before_fallback:
        reader.read(
            capabilities,
            REFERENCE_TIME,
            environment=environment,
            runner=before_runner,
        )
    assert before_fallback.value.code is (
        ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK
    )
    assert before_runner.calls == []


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
        None,
        None,
        token_suffix="keychain-a-secret",
        access_expires_at=_FUTURE_EXPIRY,
    )
    payload_b = credential_payload(
        None,
        None,
        token_suffix="keychain-b-secret",
        access_expires_at=_FUTURE_EXPIRY,
    )
    environment = {"USER": "sidekick-test"}
    service_a = _keychain_service(profile_a)
    service_b = _keychain_service(profile_b)
    native_arguments = _keychain_arguments("Claude Code-credentials")
    profile_a_arguments = _keychain_arguments(service_a)
    profile_b_arguments = _keychain_arguments(service_b)
    status_arguments = ("auth", "status")
    claude_executable = Path(sys.executable).resolve()
    runner = ClaudeRunner(
        {
            native_arguments: ClaudeCommandResult(0, payload_a + b"\n"),
            profile_a_arguments: ClaudeCommandResult(0, payload_a + b"\r\n"),
            profile_b_arguments: ClaudeCommandResult(0, payload_b + b"\n"),
        },
        profile_responses=claude_profile_status_responses(
            {
                native.config_directory: "native",
                profile_a.config_directory: "profile-a",
                profile_b.config_directory: "profile-b",
            }
        ),
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
        (claude_executable, status_arguments),
        (_KEYCHAIN_EXECUTABLE, native_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_a_arguments),
        (claude_executable, status_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_a_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_b_arguments),
        (claude_executable, status_arguments),
        (_KEYCHAIN_EXECUTABLE, profile_b_arguments),
    ]
    keychain_indexes = (0, 2, 3, 5, 6, 8)
    assert (
        tuple(runner.timeouts[index] for index in keychain_indexes)
        == (KEYCHAIN_READ_TIMEOUT_SECONDS,) * 6
    )
    assert (
        tuple(runner.output_limits[index] for index in keychain_indexes)
        == (CLAUDE_CREDENTIAL_BYTES + 2,) * 6
    )
    assert (
        tuple(runner.environments[index] for index in keychain_indexes)
        == ({"PATH": os.defpath, "USER": "sidekick-test"},) * 6
    )
    assert "keychain-a-secret" not in repr(snapshot_a)
    assert "keychain-b-secret" not in repr(snapshot_b)

    _prove_keychain_failure_contract(
        reader,
        capabilities_a,
        profile_a_arguments,
        environment,
        profiles,
        payload_a,
    )
