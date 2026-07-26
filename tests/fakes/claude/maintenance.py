"""Synthetic managed-Claude maintenance scenarios."""

import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import ClaudeSetupTokenCredentials
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    OperationKind,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationRuntime,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.service import (
    ClaudeManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.credentials.claude.setup.service import (
    ClaudeSetupTokenCoordinator,
)
from sidekick_usages.daemon.lifecycle.readiness import SupervisorReadiness
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.claude.maintenance import (
    ClaudeManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeNativeProfile,
)
from tests.fakes.claude.managed import (
    CLAUDE_LOGGED_OUT_STATUS,
    CLAUDE_LOGIN_HELP_OUTPUT,
    CLAUDE_VERSION_OUTPUT,
    ClaudeManagedLoginScript,
    ClaudeRunner,
    claude_capabilities,
    credential_payload,
    managed_profile,
    native_profile,
    profile_tree,
)
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME, FixedClock

ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
AUTHORITY_A = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
AUTHORITY_B = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SETUP_AUTHORITY = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
INITIAL_EXPIRY = REFERENCE_TIME + timedelta(minutes=10)
FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
MANAGED_LOGIN_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CONFIG_DIR",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "USER",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
    }
)
NATIVE_LOGIN_ENVIRONMENT_KEYS = MANAGED_LOGIN_ENVIRONMENT_KEYS - {
    "CLAUDE_CONFIG_DIR"
}
PRIVATE_PROCESS_UMASK = 0o077 if os.name == "posix" else -1


@dataclass(frozen=True, slots=True)
class ResolverScenario:
    """Two managed accounts with one selected native authority."""

    paths: ApplicationPaths
    store: AccountStore
    clock: FixedClock
    selected_account: SavedAccount
    inactive_account: SavedAccount
    runner: ClaudeRunner
    environment: dict[str, str]
    runtime: ClaudeActivationRuntime


@dataclass(frozen=True, slots=True)
class MaintenanceScenario:
    """Independent private and selected-native maintenance state."""

    paths: ApplicationPaths
    store: AccountStore
    profiles: PrivateCredentialTree
    original: tuple[SavedAccount, ...]
    coordinator: ClaudeManagedAuthorityCoordinator
    clock: FixedClock
    runner: ClaudeRunner
    profile_a: Path
    profile_b: Path
    native_profile: ClaudeNativeProfile
    native_file: Path
    selected_before: SelectedAccountState
    payload_a: bytes
    refreshed_private_b: bytes
    refreshed_native_b: bytes
    unsafe_parent: dict[str, str]


@dataclass(frozen=True, slots=True)
class UnverifiedGenerationScenario:
    """One private account whose official login does not rotate state."""

    paths: ApplicationPaths
    store: AccountStore
    profiles: PrivateCredentialTree
    original: SavedAccount
    coordinator: ClaudeManagedAuthorityCoordinator
    payload: bytes


@dataclass(frozen=True, slots=True)
class ManagedLoginRecord:
    """One captured official-login process boundary."""

    executable: Path
    environment: dict[str, str]
    working_directory: Path | None
    timeout_seconds: float
    maximum_output_bytes: int
    umask: int


def resolver_scenario(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ResolverScenario:
    """Build selected-native and inactive-private resolver state."""
    paths, store, _, _ = _seed_managed_accounts(
        root / "state",
        (
            (
                ACCOUNT_A,
                AUTHORITY_A,
                AccountLabel("claude-a"),
                credential_payload(
                    "provider-account-a",
                    "provider-organization-a",
                    token_suffix="account-a-private",
                    access_expires_at=FUTURE_EXPIRY,
                ),
            ),
            (
                ACCOUNT_B,
                AUTHORITY_B,
                AccountLabel("claude-b"),
                credential_payload(
                    "provider-account-b",
                    "provider-organization-b",
                    token_suffix="account-b-private",
                    access_expires_at=FUTURE_EXPIRY,
                ),
            ),
        ),
    )
    native_payload = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="account-a-native",
        access_expires_at=FUTURE_EXPIRY,
    )
    _select_native_account(
        paths,
        root / "native-home",
        native_payload,
        ACCOUNT_A,
    )
    account_a = store.read_saved(ACCOUNT_A)
    account_b = store.read_saved(ACCOUNT_B)
    if account_a is None or account_b is None:
        raise AssertionError("Expected managed Claude accounts.")
    if not isinstance(account_a.authority, ClaudeAccountAuthority):
        raise AssertionError("Expected Claude authority.")
    clock = FixedClock()
    selected = ClaudeSetupTokenCoordinator(
        store,
        clock,
        authority_id_factory=lambda: SETUP_AUTHORITY,
    ).save(
        account_a,
        ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-account-a-setup"
        ),
    )
    _install_claude_executable(monkeypatch)
    runner = ClaudeRunner(
        {
            ("--version",): ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT),
            ("auth", "status"): ClaudeCommandResult(
                1,
                CLAUDE_LOGGED_OUT_STATUS,
            ),
            ("auth", "login", "--help"): ClaudeCommandResult(
                0,
                CLAUDE_LOGIN_HELP_OUTPUT,
            ),
        }
    )
    environment = {
        "HOME": str(root / "native-home"),
        "PATH": os.environ["PATH"],
        "USER": "sidekick-test",
    }
    runtime = ClaudeActivationRuntime(
        environment=environment,
        host=HostPlatform.LINUX,
        runner=runner,
    )
    return ResolverScenario(
        paths,
        store,
        clock,
        selected,
        account_b,
        runner,
        environment,
        runtime,
    )


def maintenance_scenario(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MaintenanceScenario:
    """Build two private accounts and one selected native authority."""
    payload_a = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="account-a-old",
        access_expires_at=INITIAL_EXPIRY,
    )
    payload_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="account-b-old",
        access_expires_at=INITIAL_EXPIRY,
    )
    refreshed_private_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="account-b-private-new",
        access_expires_at=FUTURE_EXPIRY,
    )
    refreshed_native_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="account-b-native-new",
        access_expires_at=FUTURE_EXPIRY,
    )
    paths, store, profiles, original = _seed_managed_accounts(
        root / "state",
        (
            (ACCOUNT_B, AUTHORITY_B, AccountLabel("claude-b"), payload_b),
            (ACCOUNT_A, AUTHORITY_A, AccountLabel("claude-a"), payload_a),
        ),
    )
    profile_a = managed_profile(paths, ACCOUNT_A).config_directory
    profile_b = managed_profile(paths, ACCOUNT_B).config_directory
    native, native_file, selected = _select_native_account(
        paths,
        root / "native-home",
        payload_b,
        ACCOUNT_B,
    )
    unsafe_parent = {
        "ANTHROPIC_API_KEY": "parent-api-secret",
        "ANTHROPIC_AUTH_TOKEN": "parent-auth-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth-secret",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "parent-refresh-secret",
        "CLAUDE_CODE_OAUTH_SCOPES": "parent:scope",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "SIDEKICK_UNRELATED_SECRET": "unrelated-parent-secret",
    }
    environment = {
        "HOME": str(native.config_directory.parent),
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "USER": "sidekick-test",
        **unsafe_parent,
    }
    _install_claude_executable(monkeypatch)
    runner = ClaudeRunner(
        script=ClaudeManagedLoginScript(
            profiles,
            {
                profile_a: (None,),
                profile_b: (refreshed_private_b,),
                native.config_directory: (refreshed_native_b,),
            },
        )
    )
    clock = FixedClock()
    coordinator = _managed_coordinator(
        paths,
        store,
        profiles,
        clock,
        environment,
        runner,
    )
    return MaintenanceScenario(
        paths,
        store,
        profiles,
        original,
        coordinator,
        clock,
        runner,
        profile_a,
        profile_b,
        native,
        native_file,
        selected,
        payload_a,
        refreshed_private_b,
        refreshed_native_b,
        unsafe_parent,
    )


def unverified_generation_scenario(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> UnverifiedGenerationScenario:
    """Build one account whose official login leaves generation unchanged."""
    payload = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="unchanged-generation",
        access_expires_at=INITIAL_EXPIRY,
    )
    paths, store, profiles, original = _seed_managed_accounts(
        root / "state",
        (
            (
                ACCOUNT_A,
                AUTHORITY_A,
                AccountLabel("claude-a"),
                payload,
            ),
        ),
    )
    profile = managed_profile(paths, ACCOUNT_A).config_directory

    _install_claude_executable(monkeypatch)
    coordinator = _managed_coordinator(
        paths,
        store,
        profiles,
        FixedClock(),
        {
            "HOME": str(root / "native-home"),
            "PATH": os.environ["PATH"],
            "USER": "sidekick-test",
        },
        ClaudeRunner(
            script=ClaudeManagedLoginScript(
                profiles,
                {profile: (payload,)},
            )
        ),
    )
    return UnverifiedGenerationScenario(
        paths,
        store,
        profiles,
        original[0],
        coordinator,
        payload,
    )


def execute_due_maintenance(
    scenario: MaintenanceScenario,
) -> tuple[tuple[SidekickAccountId, WorkerOutcome], ...]:
    """Execute each due managed maintenance operation in queue order."""
    SupervisorReadiness(scenario.paths, scenario.clock).enroll_accounts()
    operations = tuple(
        operation
        for operation in OperationQueueStore(
            scenario.paths.durable_operations
        ).due(scenario.clock.now())
        if operation.kind is OperationKind.MAINTAIN
    )
    executor = ClaudeManagedMaintenanceWorkerExecutor(
        scenario.coordinator,
        scenario.clock,
    )
    outcomes: list[tuple[SidekickAccountId, WorkerOutcome]] = []
    for operation in operations:
        with OperationAuthorityLock(
            scenario.paths.durable_operations,
            operation.required_account_id,
        ).hold() as authority:
            outcomes.append(
                (
                    operation.required_account_id,
                    executor.execute(operation, authority).outcome,
                )
            )
    return tuple(outcomes)


def managed_login_records(
    runner: ClaudeRunner,
) -> tuple[ManagedLoginRecord, ...]:
    """Return captured official-login calls with required environments."""
    records: list[ManagedLoginRecord] = []
    for (
        (executable, arguments),
        environment,
        working_directory,
        timeout,
        limit,
        umask,
    ) in zip(
        runner.calls,
        runner.environments,
        runner.working_directories,
        runner.timeouts,
        runner.output_limits,
        runner.umasks,
        strict=True,
    ):
        if arguments != ("auth", "login", "--claudeai"):
            continue
        if environment is None:
            raise AssertionError("Claude login environment is unavailable.")
        records.append(
            ManagedLoginRecord(
                executable,
                environment,
                working_directory,
                timeout,
                limit,
                umask,
            )
        )
    return tuple(records)


def claude_login_profile(environment: dict[str, str]) -> Path:
    """Resolve the exact private or native profile from a child environment."""
    configured = environment.get("CLAUDE_CONFIG_DIR")
    return (
        Path(configured)
        if configured is not None
        else Path(environment["HOME"]) / ".claude"
    )


def _seed_managed_accounts(
    root: Path,
    entries: tuple[
        tuple[SidekickAccountId, AuthorityId, AccountLabel, bytes],
        ...,
    ],
) -> tuple[
    ApplicationPaths,
    AccountStore,
    PrivateCredentialTree,
    tuple[SavedAccount, ...],
]:
    paths = make_application_paths(root)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    profiles = profile_tree(paths)
    reader = ClaudeManagedAuthorityReader(paths, profiles)
    accounts: list[SavedAccount] = []
    for account_id, authority_id, label, payload in entries:
        profile = managed_profile(paths, account_id)
        profiles.ensure_owned_directory(profile.config_directory)
        profiles.write_owned_file(
            profile.config_directory,
            CLAUDE_CREDENTIAL_FILE,
            payload,
        )
        snapshot = reader.read(
            claude_capabilities(
                profile,
                ClaudeManagedPlatform.LINUX_FILE,
            ),
            REFERENCE_TIME,
        )
        accounts.append(
            SavedAccount(
                account_id=account_id,
                label=label,
                provider_id=ProviderId.CLAUDE,
                plan=snapshot.plan,
                authority=ClaudeAccountAuthority(
                    subscription=managed_login_authority(
                        snapshot,
                        authority_id,
                        REFERENCE_TIME - timedelta(minutes=5),
                    )
                ),
                credential_health=snapshot.health,
            )
        )
    persisted = tuple(accounts)
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            encode_version_three(VersionThreeDocument(persisted)),
            AuthorityExpectation.ABSENT,
        )
    credentials = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return (
        paths,
        AccountStore(paths.accounts, credentials).load(),
        profiles,
        persisted,
    )


def _managed_coordinator(
    paths: ApplicationPaths,
    store: AccountStore,
    profiles: PrivateCredentialTree,
    clock: FixedClock,
    environment: dict[str, str],
    runner: ClaudeRunner,
) -> ClaudeManagedAuthorityCoordinator:
    capabilities = ClaudeProfileCapabilityFactory(
        paths,
        profiles,
        environment=environment,
        host=HostPlatform.LINUX,
        runner=runner,
    )
    activation = ClaudeActivationAuthorityCoordinator(
        paths,
        store,
        profiles,
        clock,
        capabilities=capabilities,
        runtime=ClaudeActivationRuntime(
            environment=environment,
            host=HostPlatform.LINUX,
            runner=runner,
        ),
    )
    return ClaudeManagedAuthorityCoordinator(
        paths,
        store,
        profiles,
        SelectedStateStore(paths.selected_state),
        activation,
        capabilities,
        clock,
        environment=environment,
        runner=runner,
    )


def _select_native_account(
    paths: ApplicationPaths,
    root: Path,
    payload: bytes,
    account_id: SidekickAccountId,
) -> tuple[ClaudeNativeProfile, Path, SelectedAccountState]:
    native = native_profile(root)
    credential_file = native.config_directory / CLAUDE_CREDENTIAL_FILE
    credential_file.write_bytes(payload)
    os.chmod(credential_file, 0o600)
    snapshot = ClaudeNativeAuthorityReader(native).read(
        claude_capabilities(
            native,
            ClaudeManagedPlatform.LINUX_FILE,
        ),
        REFERENCE_TIME,
    )
    selected = SelectedStateStore(paths.selected_state).save(
        SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=account_id,
            provider_identity=snapshot.provider_identity,
            runtime_generation=snapshot.generation,
            verified_at=REFERENCE_TIME,
            outcome=ActivationOutcome.VERIFIED,
        )
    )
    return native, credential_file, selected


def _install_claude_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )
