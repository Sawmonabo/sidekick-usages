"""Load-bearing Claude managed-migration scenarios."""

import os
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    ClaudeStoredLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialHealth,
)
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    AccountUsageSnapshot,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.types import ActivationPhase
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.claude.managed.migration.models import (
    ClaudeMigrationRuntime,
)
from sidekick_usages.credentials.claude.managed.migration.service import (
    ClaudeManagedMigrationCoordinator,
)
from sidekick_usages.credentials.claude.setup.service import (
    ClaudeSetupTokenCoordinator,
)
from sidekick_usages.credentials.models import CredentialLoginSuccess
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
)
from tests.fakes.claude.activation import claude_activation_scenario
from tests.fakes.claude.managed import (
    ClaudeManagedLoginScript,
    ClaudeRunner,
    credential_payload,
    managed_profile,
    profile_tree,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_account_store_with_private,
    make_application_paths,
)

_OLD_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(minutes=30)
_INTERACTIVE_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_NEW_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=2)
_SETUP_AUTHORITY_ID = AuthorityId("33333333-3333-4333-8333-333333333333")


class _SimulatedCrash(BaseException):
    """Stop one migration at an exact durable boundary."""


def _setup_authority_id() -> AuthorityId:
    """Return the deterministic setup authority used by this contract."""
    return _SETUP_AUTHORITY_ID


def _identity(suffix: str) -> ClaudeLoginIdentity:
    """Return one complete synthetic provider identity."""
    return ClaudeLoginIdentity(
        account_id=f"provider-account-{suffix}",
        organization_id=f"provider-organization-{suffix}",
    )


def _legacy_account(
    label: str,
    suffix: str,
    *,
    heartbeat: bool = False,
) -> Account:
    """Build one identity-bound legacy Claude subscription."""
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeLoginCredentials(
            access_token=f"sk-ant-oat01-{suffix}-old",
            refresh_token=f"refresh-{suffix}-old",
            access_expiry=KnownExpiry(_OLD_ACCESS_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile", "user:inference"),
            identity=_identity(suffix),
        ),
        plan="team",
        heartbeat_enabled=heartbeat,
        heartbeat_window_resets=(
            {"five-hour": REFERENCE_TIME + timedelta(hours=1)}
            if heartbeat
            else None
        ),
        heartbeat_targets=("five-hour",) if heartbeat else None,
        last_heartbeat_at=REFERENCE_TIME if heartbeat else None,
        last_heartbeat_status=(HeartbeatStatus.WARMED if heartbeat else None),
    )


def _attach_setup_token(
    store: AccountStore,
    clock: FixedClock,
    account: SavedAccount,
    suffix: str,
) -> SavedAccount:
    """Attach one independent setup-token authority to a saved login."""
    return ClaudeSetupTokenCoordinator(
        store,
        clock,
        authority_id_factory=_setup_authority_id,
    ).save(
        account,
        ClaudeSetupTokenCredentials(
            access_token=f"sk-ant-oat01-{suffix}-setup"
        ),
    )


def _dual_account(
    store: AccountStore,
    clock: FixedClock,
    account: SavedAccount,
    suffix: str,
) -> tuple[
    SavedAccount,
    ClaudeAccountAuthority,
    ClaudeStoredLoginAuthority,
]:
    """Return one saved account with both independent Claude authorities."""
    saved = _attach_setup_token(store, clock, account, suffix)
    authority = saved.authority
    if not isinstance(authority, ClaudeAccountAuthority):
        raise AssertionError("Expected Claude account authority.")
    subscription = authority.subscription
    if authority.setup_token is None or not isinstance(
        subscription, ClaudeStoredLoginAuthority
    ):
        raise AssertionError("Expected dual stored Claude authorities.")
    return saved, authority, subscription


def _stored_subscription(account: SavedAccount) -> ClaudeStoredLoginAuthority:
    """Return one expected stored Claude subscription."""
    authority = account.authority
    if not isinstance(authority, ClaudeAccountAuthority) or not isinstance(
        authority.subscription,
        ClaudeStoredLoginAuthority,
    ):
        raise AssertionError("Expected stored Claude subscription.")
    return authority.subscription


def _stored_payloads(
    repository: CredentialAuthorityRepository,
    account: SavedAccount,
    authority: ClaudeAccountAuthority,
    subscription: ClaudeStoredLoginAuthority,
) -> tuple[bytes, bytes]:
    """Read exact setup and subscription payloads for one dual account."""
    setup = authority.setup_token
    if setup is None:
        raise AssertionError("Expected setup-token authority.")
    setup_payload = repository.read_payload(
        account.account_id,
        setup.authority_id,
    )
    subscription_payload = repository.read_payload(
        account.account_id,
        subscription.authority_id,
    )
    if setup_payload is None or subscription_payload is None:
        raise AssertionError("Expected protected authority payloads.")
    return setup_payload, subscription_payload


def _usage_snapshot(account: SavedAccount) -> AccountUsageSnapshot:
    """Return one unbound metric snapshot for identity promotion."""
    return AccountUsageSnapshot(
        account_id=account.account_id,
        provider_id=ProviderId.CLAUDE,
        provider_identity=None,
        plan=account.plan,
        report=UsageReport(
            windows=(
                UsageWindow(
                    "5h",
                    0.42,
                    REFERENCE_TIME + timedelta(hours=1),
                ),
            ),
            plan=account.plan,
        ),
        fetched_at=REFERENCE_TIME,
    )


def _assert_pending_usage(
    snapshots: UsageSnapshotStore,
    account: SavedAccount,
    original: AccountUsageSnapshot,
) -> AccountUsageSnapshot:
    """Require visible metrics backed by one pending identity intent."""
    expected = replace(
        original,
        provider_identity=account.provider_identity,
    )
    assert snapshots.pending_identity_promotions(ProviderId.CLAUDE) == (
        account.account_id,
    )
    assert snapshots.load(account) == expected
    return expected


def _managed_account(
    store: AccountStore,
    original: SavedAccount,
    setup_authority: ClaudeSetupTokenAuthority,
) -> SavedAccount:
    """Return the migrated account after proving both authorities."""
    current = store.read_saved(original.account_id)
    if current is None or not isinstance(
        current.authority,
        ClaudeAccountAuthority,
    ):
        raise AssertionError("Expected migrated Claude account.")
    if not isinstance(
        current.authority.subscription,
        ClaudeManagedLoginAuthority,
    ):
        raise AssertionError("Expected managed Claude subscription.")
    assert current.authority.setup_token == setup_authority
    return current


def _use_synthetic_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the synthetic exact executable for managed tests."""
    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )


def _coordinator(
    paths: ApplicationPaths,
    store: AccountStore,
    private: PrivateCredentialTree,
    profiles: PrivateCredentialTree,
    snapshots: UsageSnapshotStore,
    clock: FixedClock,
    script: ClaudeManagedLoginScript,
    native_profile: Path,
) -> ClaudeManagedMigrationCoordinator:
    """Compose one migration service around isolated test boundaries."""
    return ClaudeManagedMigrationCoordinator(
        paths,
        store,
        credential_resolver_for(store, private),
        profiles,
        snapshots,
        clock,
        runtime=ClaudeMigrationRuntime(
            environment={
                "CLAUDE_CONFIG_DIR": str(native_profile),
                "PATH": os.defpath,
                "USER": "sidekick-test",
            },
            host=HostPlatform.LINUX,
            runner=ClaudeRunner(script=script),
            interactive_runner=script.interactive,
        ),
    )


def _native_sentinel(root: Path) -> Path:
    """Create one native-login marker outside managed profiles."""
    root.mkdir()
    sentinel = root / "native-login"
    sentinel.write_bytes(b"native-login-must-remain")
    return sentinel


def test_two_account_migration_preserves_dual_authority_and_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mismatch remains intact while the other account commits."""
    _use_synthetic_claude(monkeypatch)
    paths = make_application_paths(tmp_path)
    store, private = make_account_store_with_private(
        tmp_path,
        (
            _legacy_account("mismatch", "a"),
            _legacy_account("dual", "b", heartbeat=True),
        ),
    )
    original_a, original_b = store.saved_accounts()
    clock = FixedClock()
    dual_before, authority_b, subscription_b = _dual_account(
        store,
        clock,
        original_b,
        "b",
    )
    subscription_a = _stored_subscription(original_a)
    repository = CredentialAuthorityRepository(private)
    legacy_a = repository.bundle_path(
        original_a.account_id,
        subscription_a.authority_id,
    )
    legacy_b = repository.bundle_path(
        dual_before.account_id,
        subscription_b.authority_id,
    )
    setup_authority_b = authority_b.setup_token
    if setup_authority_b is None:
        raise AssertionError("Expected setup-token authority.")
    setup_b = repository.bundle_path(
        dual_before.account_id,
        setup_authority_b.authority_id,
    )
    snapshots = UsageSnapshotStore(paths.usage_snapshots)
    usage_before = snapshots.save(_usage_snapshot(dual_before))
    profiles = profile_tree(paths)
    profile_a = managed_profile(
        paths,
        original_a.account_id,
    ).config_directory
    profile_b = managed_profile(
        paths,
        dual_before.account_id,
    ).config_directory
    script = ClaudeManagedLoginScript(
        profiles,
        {
            profile_a: (
                credential_payload(
                    "provider-account-other",
                    "provider-organization-other",
                    token_suffix="a-wrong",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
            ),
            profile_b: (
                None,
                credential_payload(
                    "provider-account-b",
                    "provider-organization-b",
                    token_suffix="b-proven",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
            ),
        },
        interactive_payloads={
            profile_b: credential_payload(
                "provider-account-b",
                "provider-organization-b",
                token_suffix="b-browser",
                access_expires_at=_INTERACTIVE_ACCESS_EXPIRY,
            ),
        },
    )
    native_profile = tmp_path / "native-claude"
    native_sentinel = _native_sentinel(native_profile)
    coordinator = _coordinator(
        paths,
        store,
        private,
        profiles,
        snapshots,
        clock,
        script,
        native_profile,
    )

    mismatch = coordinator.migrate(
        original_a.label,
        establish_identity=False,
        interactive=False,
    )
    migrated = coordinator.migrate(
        dual_before.label,
        establish_identity=False,
        interactive=True,
    )

    assert isinstance(mismatch, ProviderFailure)
    assert mismatch.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert isinstance(migrated, CredentialLoginSuccess)
    current_a = store.read_saved(original_a.account_id)
    current_b = store.read_saved(dual_before.account_id)
    assert current_a == original_a
    assert current_b is not None
    current_authority = current_b.authority
    assert isinstance(current_authority, ClaudeAccountAuthority)
    managed = current_authority.subscription
    assert isinstance(managed, ClaudeManagedLoginAuthority)
    assert current_authority.setup_token == authority_b.setup_token
    assert managed.authority_id == subscription_b.authority_id
    assert (
        current_b.account_id,
        current_b.heartbeat_enabled,
        current_b.heartbeat_window_resets,
        current_b.heartbeat_targets,
        current_b.last_heartbeat_at,
        current_b.last_heartbeat_status,
        current_b.credential_health,
    ) == (
        dual_before.account_id,
        dual_before.heartbeat_enabled,
        dual_before.heartbeat_window_resets,
        dual_before.heartbeat_targets,
        dual_before.last_heartbeat_at,
        dual_before.last_heartbeat_status,
        CredentialHealth.HEALTHY,
    )
    assert snapshots.load(current_b) == replace(
        usage_before,
        provider_identity=_identity("b").provider_identity,
    )
    assert {account.label for account in store.saved_accounts()} == {
        AccountLabel("mismatch"),
        AccountLabel("dual"),
    }
    assert legacy_a.is_dir()
    assert not legacy_b.exists()
    assert setup_b.is_dir()
    assert script.login_profiles == [profile_a, profile_b, profile_b]
    assert script.interactive_profiles == [profile_b]
    assert native_sentinel.read_bytes() == b"native-login-must-remain"


def test_interrupted_commit_recovers_profile_before_retiring_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both commit gaps recover without losing authority or metrics."""
    _use_synthetic_claude(monkeypatch)
    paths = make_application_paths(tmp_path)
    store, private = make_account_store_with_private(
        tmp_path,
        (_legacy_account("recover", "recover"),),
    )
    original, authority, subscription = _dual_account(
        store,
        FixedClock(),
        store.saved_accounts()[0],
        "recover",
    )
    setup_authority = authority.setup_token
    if setup_authority is None:
        raise AssertionError("Expected setup-token authority.")
    repository = CredentialAuthorityRepository(private)
    legacy = repository.bundle_path(
        original.account_id,
        subscription.authority_id,
    )
    protected_payloads = _stored_payloads(
        repository,
        original,
        authority,
        subscription,
    )
    index_payload = paths.accounts.read_bytes()
    snapshots = UsageSnapshotStore(paths.usage_snapshots)
    usage_before = snapshots.save(_usage_snapshot(original))
    profiles = profile_tree(paths)
    profile = managed_profile(
        paths,
        original.account_id,
    ).config_directory
    script = ClaudeManagedLoginScript(
        profiles,
        {
            profile: (
                credential_payload(
                    "provider-account-recover",
                    "provider-organization-recover",
                    token_suffix="recover-new",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
            )
        },
    )
    coordinator = _coordinator(
        paths,
        store,
        private,
        profiles,
        snapshots,
        FixedClock(),
        script,
        tmp_path / "unused-native-profile",
    )
    commit = store.migrate_stored_authority

    def stop_before_authority_commit(
        account: SavedAccount,
        *,
        expected: SavedAccount,
    ) -> None:
        del account, expected
        raise _SimulatedCrash

    monkeypatch.setattr(
        store,
        "migrate_stored_authority",
        stop_before_authority_commit,
    )
    with pytest.raises(_SimulatedCrash):
        coordinator.migrate(
            original.label,
            establish_identity=False,
            interactive=False,
        )

    assert paths.accounts.read_bytes() == index_payload
    assert store.read_saved(original.account_id) == original
    assert (
        _stored_payloads(
            repository,
            original,
            authority,
            subscription,
        )
        == protected_payloads
    )
    protected_profile = profiles.read_owned_file(
        profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    assert protected_profile is not None
    _assert_pending_usage(snapshots, original, usage_before)
    monkeypatch.setattr(store, "migrate_stored_authority", commit)

    promote_identity = snapshots.promote_identity

    def stop_before_usage_promotion(
        account_id: object,
        provider_id: object,
        provider_identity: object,
    ) -> None:
        del account_id, provider_id, provider_identity
        raise _SimulatedCrash

    monkeypatch.setattr(
        snapshots,
        "promote_identity",
        stop_before_usage_promotion,
    )
    with pytest.raises(_SimulatedCrash):
        coordinator.migrate(
            original.label,
            establish_identity=False,
            interactive=False,
        )

    current = _managed_account(store, original, setup_authority)
    assert (
        repository.read_payload(
            current.account_id,
            setup_authority.authority_id,
        )
        == protected_payloads[0]
    )
    assert not legacy.exists()
    expected_usage = _assert_pending_usage(snapshots, current, usage_before)
    assert script.login_profiles == [profile]

    monkeypatch.setattr(snapshots, "promote_identity", promote_identity)
    recovered = coordinator.migrate(
        original.label,
        establish_identity=False,
        interactive=False,
    )

    assert isinstance(recovered, CredentialLoginSuccess)
    assert snapshots.pending_identity_promotions(ProviderId.CLAUDE) == ()
    assert snapshots.load(current) == expected_usage
    assert script.login_profiles == [profile]


def test_native_activation_retains_source_and_commits_verified_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One official A-to-B switch preserves both private authorities."""
    _use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)

    with ProviderMutationLock(
        scenario.paths.durable_operations,
        ProviderId.CLAUDE,
        (scenario.source.account_id, scenario.target.account_id),
        timeout_seconds=1.0,
    ).hold() as authority:
        result = scenario.executor.execute(scenario.operation, authority)

    assert result.outcome is WorkerOutcome.SUCCEEDED
    current_source = scenario.store.read_saved(scenario.source.account_id)
    assert current_source is not None
    current_source_authority = current_source.authority
    assert isinstance(current_source_authority, ClaudeAccountAuthority)
    current_source_subscription = current_source_authority.subscription
    assert isinstance(
        current_source_subscription,
        ClaudeManagedLoginAuthority,
    )
    assert current_source_subscription.generation == (
        claude_access_token_generation("sk-ant-oat01-source-retained")
    )
    retained = scenario.profiles.read_owned_file(
        scenario.source_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    unchanged_target = scenario.profiles.read_owned_file(
        scenario.target_profile,
        CLAUDE_CREDENTIAL_FILE,
    )
    assert retained is not None
    assert retained.data == scenario.retained_source_payload
    assert unchanged_target is not None
    assert unchanged_target.data == scenario.target_payload
    assert (
        scenario.native_credentials.read_bytes()
        == scenario.native_target_payload
    )
    assert scenario.script.login_profiles == [
        scenario.source_profile,
        scenario.native.config_directory,
    ]
    login_environments = [
        environment
        for (_executable, arguments), environment in zip(
            scenario.runner.calls,
            scenario.runner.environments,
            strict=True,
        )
        if arguments == ("auth", "login", "--claudeai")
    ]
    assert login_environments[0] is not None
    assert login_environments[1] is not None
    assert login_environments[0][CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY] == str(
        scenario.source_profile
    )
    assert CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY not in login_environments[1]
    claude_state = scenario.selected.load(ProviderId.CLAUDE)
    assert claude_state is not None
    assert claude_state.account_id == scenario.target.account_id
    assert claude_state.provider_identity == (
        scenario.target.provider_identity
    )
    assert scenario.selected.load(ProviderId.CODEX) == scenario.codex_state
    journal = scenario.journals.load(ProviderId.CLAUDE)
    assert journal.active is None
    assert len(journal.history) == 1
    committed = journal.history[0]
    assert committed.phase is ActivationPhase.COMMITTED
    assert committed.target_account_id == scenario.target.account_id
    assert (
        committed.verified_runtime_generation
        == claude_state.runtime_generation
    )
