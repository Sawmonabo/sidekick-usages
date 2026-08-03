"""Managed Claude capability and protected-profile boundary tests."""

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import NoReturn

import pytest

import sidekick_usages.persistence.platform.posix.files
import sidekick_usages.persistence.platform.posix.mounts
import sidekick_usages.platform.executable
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialAction,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.capabilities.service import (
    ProviderCapabilityService,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_authority_matches,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
    prepare_claude_managed_profile,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.types import FilesystemFamily
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    CLAUDE_CREDENTIAL_BYTES,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    MINIMUM_CLAUDE_VERSION,
    discover_claude_executable_from_launcher,
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredActivityKind,
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredCapability,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
)
from sidekick_usages.providers.claude.structured.process import (
    CLAUDE_STRUCTURED_EMBEDDED_BUILD_TIME,
    CLAUDE_STRUCTURED_EMBEDDED_GIT_SHA,
    ClaudeStructuredProcess,
    qualify_claude_structured_capability,
)
from tests.fakes.claude.managed import (
    CLAUDE_LOGGED_OUT_STATUS,
    CLAUDE_LOGIN_HELP_OUTPUT,
    CLAUDE_VERSION_OUTPUT,
    ClaudeRunner,
    ClaudeStructuredEngineFake,
    StructuredCapabilityMutation,
    StructuredResponseCase,
    claude_auth_status_payload,
    claude_auth_status_result,
    claude_capabilities,
    claude_status_identity,
    credential_payload,
    managed_profile,
    native_profile,
    profile_tree,
    structured_capability_fixture,
    structured_session_fixture,
)
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME

if sys.platform == "darwin":
    import sidekick_usages.persistence.platform.macos.adapter

_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_PRIVATE_DIRECTORY_MODE = 0o700
_AUTHORITY_ID = AuthorityId("33333333-3333-4333-8333-333333333333")
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)


def _probe_runner(
    *,
    login_return_code: int = 0,
    login_help_output: bytes = CLAUDE_LOGIN_HELP_OUTPUT,
    version_output: bytes = CLAUDE_VERSION_OUTPUT,
) -> ClaudeRunner:
    return ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, version_output),
            ("auth", "status"): ClaudeCommandResult(
                1,
                CLAUDE_LOGGED_OUT_STATUS,
            ),
            ("auth", "login", "--help"): ClaudeCommandResult(
                login_return_code,
                login_help_output,
            ),
        }
    )


def _native_failure(
    reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    runner: ClaudeRunner,
) -> ClaudeProtectedStorageFailure:
    """Return one safe failure from the native credential boundary."""
    with pytest.raises(ClaudeProtectedStorageError) as failure:
        reader.read(capabilities, REFERENCE_TIME, runner=runner)
    return failure.value.code


def _prove_provider_parent_contract(
    reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    provider_directory: Path,
    runner: ClaudeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove held qualification, parent policy, and no-follow identity."""
    failure = partial(_native_failure, reader, capabilities, runner)
    qualified_paths: list[Path] = []
    if sys.platform.startswith("linux"):
        mount_adapter = sidekick_usages.persistence.platform.posix.mounts
        original_qualify = mount_adapter.filesystem_for_descriptor

        def record_qualified_directory(
            descriptor: int,
        ) -> FilesystemFamily:
            qualified_paths.append(
                Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            )
            return original_qualify(descriptor)

        with monkeypatch.context() as changes:
            changes.setattr(
                sidekick_usages.persistence.platform.posix.mounts,
                "filesystem_for_descriptor",
                record_qualified_directory,
            )
            reader.read(capabilities, REFERENCE_TIME, runner=runner)
        assert qualified_paths == [provider_directory.resolve()] * 2

    provider_directory.chmod(0o775)
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    provider_directory.chmod(0o755)

    real_directory = provider_directory.with_name(".claude-real")
    provider_directory.rename(real_directory)
    provider_directory.symlink_to(
        real_directory.name,
        target_is_directory=True,
    )
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    provider_directory.unlink()
    real_directory.rename(provider_directory)


def _prove_provider_file_contract(
    reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    credential_path: Path,
    payload: bytes,
    runner: ClaudeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove file modes, identity, size, device, and exact-name policy."""
    failure = partial(_native_failure, reader, capabilities, runner)
    credential_path.chmod(0o644)
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    credential_path.chmod(0o600)

    linked_path = credential_path.with_name("credential-hard-link")
    os.link(credential_path, linked_path)
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    linked_path.unlink()

    moved_path = credential_path.with_name("credentials-real")
    alias_path = credential_path.with_name(".CREDENTIALS.JSON")
    credential_path.rename(moved_path).rename(alias_path)
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    alias_path.rename(moved_path).rename(credential_path)

    credential_path.rename(moved_path)
    credential_path.symlink_to(moved_path.name)
    assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    credential_path.unlink()
    moved_path.rename(credential_path)

    credential_path.write_bytes(b"x" * (CLAUDE_CREDENTIAL_BYTES + 1))
    credential_path.chmod(0o600)
    assert failure() is ClaudeProtectedStorageFailure.MALFORMED
    credential_path.write_bytes(payload)
    credential_path.chmod(0o600)

    original_read = (
        sidekick_usages.persistence.platform.posix.files.read_descriptor
    )

    def read_with_cross_device(
        descriptor: int,
        directory_device: int,
        limit: int,
        *,
        allow_interrupted_link: bool = True,
    ) -> NativeFile:
        return original_read(
            descriptor,
            directory_device + 1,
            limit,
            allow_interrupted_link=allow_interrupted_link,
        )

    with monkeypatch.context() as changes:
        changes.setattr(
            sidekick_usages.persistence.platform.posix.files,
            "read_descriptor",
            read_with_cross_device,
        )
        assert failure() is ClaudeProtectedStorageFailure.UNSAFE


def _prove_provider_change_and_entry_bound(
    reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    credential_path: Path,
    payload: bytes,
    runner: ClaudeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove concurrent replacement and bounded directory scanning."""
    failure = partial(_native_failure, reader, capabilities, runner)
    original_read = (
        sidekick_usages.persistence.platform.posix.files.read_descriptor
    )

    def replace_after_read(
        descriptor: int,
        directory_device: int,
        limit: int,
        *,
        allow_interrupted_link: bool = True,
    ) -> NativeFile:
        result = original_read(
            descriptor,
            directory_device,
            limit,
            allow_interrupted_link=allow_interrupted_link,
        )
        credential_path.unlink()
        credential_path.write_bytes(payload)
        credential_path.chmod(0o600)
        return result

    with monkeypatch.context() as changes:
        changes.setattr(
            sidekick_usages.persistence.platform.posix.files,
            "read_descriptor",
            replace_after_read,
        )
        assert failure() is ClaudeProtectedStorageFailure.UNREADABLE

    overflow_paths = tuple(
        credential_path.parent / f"entry-{index:04d}" for index in range(4_096)
    )
    for path in overflow_paths:
        path.touch(mode=0o600)
    assert failure() is ClaudeProtectedStorageFailure.MALFORMED
    for path in overflow_paths:
        path.unlink()


def _prove_macos_provider_contract(
    reader: ClaudeNativeAuthorityReader,
    capabilities: ClaudeCapabilities,
    runner: ClaudeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove APFS and parent/file extended-ACL rejection on macOS."""
    if sys.platform != "darwin":
        return
    failure = partial(_native_failure, reader, capabilities, runner)
    with monkeypatch.context() as changes:
        changes.setattr(
            sidekick_usages.persistence.platform.macos.adapter,
            "_filesystem_name",
            lambda _descriptor: "not-apfs",
        )
        assert failure() is ClaudeProtectedStorageFailure.UNSAFE
    for rejected_mode in (stat.S_IFDIR, stat.S_IFREG):
        with monkeypatch.context() as changes:
            changes.setattr(
                sidekick_usages.persistence.platform.macos.adapter,
                "has_extended_acl",
                lambda descriptor, mode=rejected_mode: (
                    stat.S_IFMT(os.fstat(descriptor).st_mode) == mode
                ),
            )
            assert failure() is ClaudeProtectedStorageFailure.UNSAFE


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
    launcher = tmp_path / "bin" / "claude"
    launcher.parent.mkdir()
    launcher.symlink_to(executable_path)
    runner = _probe_runner()
    source_environment = {
        "ANTHROPIC_API_KEY": "synthetic-native-api-key",
        "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-native-oauth",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "native-config"),
        "PATH": "/synthetic/empty-bin",
    }

    def reject_path_resolution(
        command: str,
        path: str | None = None,
    ) -> str:
        del command, path
        raise AssertionError("Explicit Claude launcher consulted PATH.")

    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        reject_path_resolution,
    )

    capabilities = ClaudeProfileCapabilityFactory(
        paths,
        profiles,
        environment=source_environment,
        host=HostPlatform.LINUX,
        runner=runner,
        executable_discovery=partial(
            discover_claude_executable_from_launcher,
            launcher,
        ),
    ).managed(_ACCOUNT_A)
    profile_a = capabilities.profile.config_directory

    expected_provenance = ExecutableProvenance.from_stat(
        executable_path,
        executable_path.stat(),
    )
    assert (
        capabilities.executable.provenance,
        capabilities.executable.launcher,
        capabilities.executable.version,
    ) == (expected_provenance, launcher, MINIMUM_CLAUDE_VERSION)
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
    assert stat.S_IMODE(profile_a.stat().st_mode) == _PRIVATE_DIRECTORY_MODE

    profiles.write_owned_file(
        profile_a,
        CLAUDE_CREDENTIAL_FILE,
        credential_payload(
            None,
            None,
            token_suffix="version",
            access_expires_at=_FUTURE_EXPIRY,
        ),
    )
    status = claude_auth_status_result(
        "profile-a@example.test",
        "provider-organization-a",
    )
    original = ClaudeManagedAuthorityReader(paths, profiles).read(
        capabilities,
        REFERENCE_TIME,
        runner=ClaudeRunner({("auth", "status"): status}),
    )
    saved_authority = managed_login_authority(
        original,
        _AUTHORITY_ID,
        REFERENCE_TIME,
    )
    saved_account = SavedAccount(
        account_id=_ACCOUNT_A,
        label=AccountLabel("synthetic-account"),
        provider_id=ProviderId.CLAUDE,
        plan=original.plan,
        authority=ClaudeAccountAuthority(subscription=saved_authority),
        credential_health=original.health,
    )

    updated_target = tmp_path / "versions" / "2.1.221" / "claude"
    updated_target.parent.mkdir(parents=True)
    updated_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    updated_target.chmod(0o755)
    launcher.unlink()
    launcher.symlink_to(updated_target)
    with pytest.raises(ClaudeManagedError) as retargeted:
        verify_claude_executable(capabilities.executable)
    assert retargeted.value.code is ClaudeManagedFailure.EXECUTABLE_UNSAFE
    updated_runner = _probe_runner(version_output=b"2.1.221 (Claude Code)\n")
    updated = discover_claude_executable_from_launcher(
        launcher,
        source_environment,
        runner=updated_runner,
    )
    assert (
        updated.launcher,
        updated.provenance.path,
        updated.version > MINIMUM_CLAUDE_VERSION,
    ) == (launcher, updated_target.resolve(), True)
    updated_snapshot = replace(
        original,
        executable_version=str(updated.version),
    )
    assert managed_authority_matches(
        saved_account,
        saved_authority,
        updated_snapshot,
    )
    assert managed_login_authority(
        updated_snapshot,
        _AUTHORITY_ID,
        REFERENCE_TIME,
    ).executable_version == str(updated.version)

    cancelled_runner = _probe_runner()
    capability_service = ProviderCapabilityService(
        ClaudeProfileCapabilityFactory(
            paths,
            profiles,
            environment=source_environment,
            host=HostPlatform.LINUX,
            runner=cancelled_runner,
            executable_discovery=partial(
                discover_claude_executable_from_launcher,
                launcher,
            ),
        ),
        source_environment,
    )
    capability_service.cancel()
    cancelled_result = capability_service.probe(ProviderId.CLAUDE)
    assert (
        cancelled_result.failure is ClaudeManagedFailure.CAPABILITY_CANCELLED
    )
    assert cancelled_runner.calls == []


@pytest.mark.parametrize(
    (
        "escaped_root",
        "login_return_code",
        "login_help_output",
        "host",
        "expected_failure",
        "expected_arguments",
    ),
    [
        (
            True,
            0,
            CLAUDE_LOGIN_HELP_OUTPUT,
            HostPlatform.LINUX,
            ClaudeManagedFailure.PROFILE_UNSAFE,
            (),
        ),
        (
            False,
            0,
            b"Usage: claude auth login [--console]\n",
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
            CLAUDE_LOGIN_HELP_OUTPUT,
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
    login_help_output: bytes,
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
    runner = _probe_runner(
        login_return_code=login_return_code,
        login_help_output=login_help_output,
    )

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    profiles = profile_tree(paths)
    profile_a = managed_profile(paths, _ACCOUNT_A)
    profiles.ensure_owned_directory(profile_a.config_directory)
    payload_a = credential_payload(
        None,
        None,
        token_suffix="profile-a",
        access_expires_at=_FUTURE_EXPIRY,
    )
    status_a = claude_auth_status_payload(
        "profile-a@example.test",
        "provider-organization-a",
    )
    runner = ClaudeRunner(
        {
            ("auth", "status"): ClaudeCommandResult(0, status_a),
        }
    )
    profiles.write_owned_file(
        profile_a.config_directory,
        CLAUDE_CREDENTIAL_FILE,
        payload_a,
    )
    expected_identity = claude_status_identity(
        "profile-a@example.test",
        "provider-organization-a",
    )
    other_identity = claude_status_identity(
        "profile-b@example.test",
        "provider-organization-b",
    )
    reader = ClaudeManagedAuthorityReader(paths, profiles)

    snapshots = tuple(
        reader.read(
            claude_capabilities(profile_a, platform),
            REFERENCE_TIME,
            expected_identity=expected_identity,
            runner=runner,
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
            runner=runner,
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
            runner=runner,
        )
    assert unsafe.value.code is ClaudeProtectedStorageFailure.UNSAFE

    native = native_profile(tmp_path / "native-home")
    native_path = native.config_directory / CLAUDE_CREDENTIAL_FILE
    native_path.write_bytes(payload_a)
    native_path.chmod(0o600)
    native.config_directory.chmod(0o755)
    native_reader = ClaudeNativeAuthorityReader(native)
    native_capabilities = claude_capabilities(
        native,
        ClaudeManagedPlatform.LINUX_FILE,
    )

    _prove_provider_parent_contract(
        native_reader,
        native_capabilities,
        native.config_directory,
        runner,
        monkeypatch,
    )
    native_snapshot = native_reader.read(
        native_capabilities,
        REFERENCE_TIME,
        expected_identity=expected_identity,
        runner=runner,
    )
    assert (
        native_snapshot.profile,
        native_snapshot.provider_identity,
    ) == (native, expected_identity)
    _prove_provider_file_contract(
        native_reader,
        native_capabilities,
        native_path,
        payload_a,
        runner,
        monkeypatch,
    )
    _prove_provider_change_and_entry_bound(
        native_reader,
        native_capabilities,
        native_path,
        payload_a,
        runner,
        monkeypatch,
    )
    _prove_macos_provider_contract(
        native_reader,
        native_capabilities,
        runner,
        monkeypatch,
    )


@pytest.mark.parametrize(
    "response_case",
    tuple(StructuredResponseCase),
)
def test_structured_session_updates_oauth_only_at_an_idle_turn_boundary(
    response_case: StructuredResponseCase,
) -> None:
    fixture = structured_session_fixture(response_case)
    session = fixture.session
    engine = fixture.engine
    process_id = session.process_id
    binding_b = fixture.binding_b
    binding_c = fixture.binding_c

    session.prepare_target(binding_b)
    ready_b = session.update_oauth(fixture.oauth_b)
    assert ready_b.binding == binding_b
    assert session.process_id == process_id

    session.begin_turn(fixture.turn_id, binding_b)
    session.prepare_target(binding_c)
    with pytest.raises(ClaudeStructuredError):
        session.update_oauth(fixture.oauth_c)
    session.end_turn(fixture.turn_id)
    for kind in ClaudeStructuredActivityKind:
        activity_id = f"synthetic-{kind.value}"
        session.begin_activity(kind, activity_id)
        with pytest.raises(ClaudeStructuredError):
            session.update_oauth(fixture.oauth_c)
        session.end_activity(kind, activity_id)

    if response_case is StructuredResponseCase.SUCCESS:
        ready_c = session.update_oauth(fixture.oauth_c)
        observed_receipts: list[ClaudeStructuredAdoptionReceipt] = []

        def transmit(receipt: ClaudeStructuredAdoptionReceipt) -> None:
            observed_receipts.append(receipt)
            engine.transmit_turn(ready_c.binding.epoch.value)

        adoption = session.route_turn(fixture.turn_id, binding_c, transmit)
        session.end_turn(fixture.turn_id)
        assert ready_c.binding == binding_c
        assert observed_receipts == [adoption]
        assert engine.events == [("adoption", "9"), ("prompt", "9")]
    else:
        with pytest.raises(ClaudeStructuredError) as failure:
            session.update_oauth(fixture.oauth_c)
        assert session.binding == binding_b
        assert fixture.oauth_c not in repr(failure.value)

    assert all(request.exact_envelope for request in engine.requests)
    assert all(request.expected_oauth for request in engine.requests)
    assert all(
        request.variable_names == ("CLAUDE_CODE_OAUTH_TOKEN",)
        for request in engine.requests
    )
    assert all(not any(buffer) for buffer in engine.cleared_request_buffers)
    assert all(engine.wiped_before_response)
    for candidate in (session, ready_b, engine):
        representation = repr(candidate)
        assert fixture.oauth_b not in representation
        assert fixture.oauth_c not in representation
    assert session.process_id == process_id


@pytest.mark.parametrize("mutation", [None, *StructuredCapabilityMutation])
def test_structured_capability_requires_the_exact_no_network_probe(
    mutation: StructuredCapabilityMutation | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = structured_capability_fixture(tmp_path, mutation)
    executable = fixture.executable
    running_engine = ClaudeStructuredEngineFake((), (), process_id=6262)
    running_process_id = running_engine.process_id

    def qualify() -> ClaudeStructuredCapability:
        return qualify_claude_structured_capability(
            executable,
            fixture.host,
            fixture.environment,
            working_directory=fixture.working_directory,
            engine_factory=fixture.factory,
            artifact_reader=fixture.inspect_artifact,
        )

    if mutation is None:
        capability = qualify()
        assert (
            capability.executable,
            capability.variable_allowlist,
            capability.embedded_build_time,
            capability.embedded_git_sha,
        ) == (
            executable,
            (
                "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
            ),
            CLAUDE_STRUCTURED_EMBEDDED_BUILD_TIME,
            CLAUDE_STRUCTURED_EMBEDDED_GIT_SHA,
        )
        assert len(fixture.factory.engines) == 1
        probe = fixture.factory.engines[0]
        assert [request.variable_names for request in probe.requests] == [
            ("CLAUDE_CODE_OAUTH_TOKEN",),
            ("CLAUDE_CODE_OAUTH_TOKEN",),
        ]
        assert all(request.exact_envelope for request in probe.requests)
        assert all(request.expected_oauth for request in probe.requests)
        assert all(probe.wiped_before_response)
        assert probe.user_turn_count == 0
        assert probe.input_closed
        assert fixture.factory.environments == [fixture.environment]

        launches: list[tuple[str, ...]] = []

        def record_launch(
            argv: tuple[str, ...],
            *,
            environment: Mapping[str, str],
            working_directory: Path,
        ) -> NoReturn:
            del environment, working_directory
            launches.append(argv)
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            )

        monkeypatch.setattr(
            "sidekick_usages.providers.claude.structured.process."
            "launch_piped_claude_command",
            record_launch,
        )
        with pytest.raises(ClaudeStructuredError):
            ClaudeStructuredProcess.open(
                capability,
                fixture.environment,
                working_directory=fixture.working_directory,
            )
        assert launches == [
            (
                str(executable.provenance.path),
                "--print",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
            )
        ]
        for arguments in (
            ("--print=true",),
            ("--model", "sonnet"),
            ("synthetic-prompt",),
        ):
            with pytest.raises(ClaudeStructuredError) as unsafe:
                ClaudeStructuredProcess.open(
                    capability,
                    fixture.environment,
                    working_directory=fixture.working_directory,
                    user_arguments=arguments,
                )
            assert (
                unsafe.value.code
                is ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            )
        assert len(launches) == 1
    else:
        with pytest.raises(ClaudeStructuredError) as failure:
            qualify()
        assert (
            failure.value.code is ClaudeStructuredFailure.VERSION_UNSUPPORTED
        )

    assert running_engine.process_id == running_process_id
    assert running_engine.requests == []
    assert running_engine.user_turn_count == 0
    assert not running_engine.input_closed
