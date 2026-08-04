"""Load-bearing Claude managed migration scenarios."""

import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

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
    ProviderIdentity,
    SidekickAccountId,
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
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from tests.fakes.claude.managed import (
    ClaudeManagedLoginScript,
    ClaudeRunner,
    claude_profile_status,
    credential_payload,
    managed_profile,
    profile_tree,
    use_synthetic_claude,
)
from tests.support.persistence import (
    make_account_store_with_private,
    make_application_paths,
)
from tests.support.time import REFERENCE_TIME, FixedClock

_OLD_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(minutes=30)
_INTERACTIVE_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_NEW_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=2)
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class _SimulatedCrash(BaseException):
    """Stop one migration at an exact durable boundary."""


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


def _setup_account(
    label: str,
    suffix: str,
    *,
    heartbeat: bool = False,
) -> Account:
    """Build one setup-only Claude account."""
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeSetupTokenCredentials(
            access_token=f"sk-ant-oat01-{suffix}-setup"
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


def _stored_subscription(account: SavedAccount) -> ClaudeStoredLoginAuthority:
    """Return one expected stored Claude subscription."""
    authority = account.authority
    if not isinstance(authority, ClaudeAccountAuthority) or not isinstance(
        authority.subscription,
        ClaudeStoredLoginAuthority,
    ):
        raise AssertionError("Expected stored Claude subscription.")
    return authority.subscription


def _setup_authority(account: SavedAccount) -> ClaudeSetupTokenAuthority:
    """Return one required setup-token authority."""
    authority = account.authority
    if (
        not isinstance(authority, ClaudeAccountAuthority)
        or authority.setup_token is None
    ):
        raise AssertionError("Expected setup-only Claude authority.")
    return authority.setup_token


def _protected_payload(
    repository: CredentialAuthorityRepository,
    account: SavedAccount,
    authority_id: AuthorityId,
) -> bytes:
    """Read one required protected test authority."""
    payload = repository.read_payload(account.account_id, authority_id)
    if payload is None:
        raise AssertionError("Expected protected authority.")
    return payload


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
) -> tuple[SavedAccount, ClaudeManagedLoginAuthority]:
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
    return current, current.authority.subscription


def _coordinator(
    paths: ApplicationPaths,
    store: AccountStore,
    private: PrivateCredentialTree,
    profiles: PrivateCredentialTree,
    snapshots: UsageSnapshotStore,
    clock: FixedClock,
    script: ClaudeManagedLoginScript,
    native_home: Path,
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
                "HOME": str(native_home),
                "PATH": os.defpath,
                "USER": "sidekick-test",
            },
            host=HostPlatform.LINUX,
            runner=ClaudeRunner(script=script),
            interactive_runner=script.interactive,
        ),
    )


def _native_sentinel(
    root: Path,
    credentials: bytes | None = None,
) -> Path:
    """Create one native-login marker outside managed profiles."""
    root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    config_directory = root / ".claude"
    config_directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    if credentials is not None:
        credential_file = config_directory / CLAUDE_CREDENTIAL_FILE
        credential_file.write_bytes(credentials)
        credential_file.chmod(_PRIVATE_FILE_MODE)
    sentinel = root / "native-login"
    sentinel.write_bytes(b"native-login-must-remain")
    return sentinel


def _failure_kind(
    result: CredentialLoginResult,
) -> ProviderFailureKind | None:
    """Return one provider failure kind without weakening result typing."""
    return result.kind if isinstance(result, ProviderFailure) else None


def test_setup_association_reconciles_without_losing_token_or_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Association rollback preserves the setup token and cached metrics."""
    use_synthetic_claude(monkeypatch)
    paths = make_application_paths(tmp_path)
    store, private = make_account_store_with_private(
        tmp_path,
        (
            _legacy_account("mismatch", "a"),
            _setup_account("dual", "b", heartbeat=True),
        ),
    )
    original_a, dual_before = store.saved_accounts()
    clock = FixedClock()
    subscription_a = _stored_subscription(original_a)
    repository = CredentialAuthorityRepository(private)
    legacy_a = repository.bundle_path(
        original_a.account_id,
        subscription_a.authority_id,
    )
    setup_authority_b = _setup_authority(dual_before)
    setup_b = repository.bundle_path(
        dual_before.account_id,
        setup_authority_b.authority_id,
    )
    setup_payload_before = _protected_payload(
        repository,
        dual_before,
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
    status_a, _association_a = claude_profile_status("a")
    status_b, association_b = claude_profile_status("b")
    native_home = tmp_path / "native-claude"
    native_config = native_home / ".claude"
    native_sentinel = _native_sentinel(
        native_home,
        credential_payload(
            None,
            None,
            token_suffix="native-b",
            access_expires_at=_NEW_ACCESS_EXPIRY,
        ),
    )
    script = ClaudeManagedLoginScript(
        profiles,
        {
            profile_b: (
                credential_payload(
                    None,
                    None,
                    token_suffix="b-first-refresh",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
                credential_payload(
                    None,
                    None,
                    token_suffix="b-second-refresh",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
            ),
        },
        interactive_payloads={
            profile_b: credential_payload(
                None,
                None,
                token_suffix="b-browser",
                access_expires_at=_INTERACTIVE_ACCESS_EXPIRY,
            ),
        },
        interactive_statuses={profile_b: (status_a, status_b)},
        profile_statuses={native_config: status_b},
    )
    coordinator = _coordinator(
        paths,
        store,
        private,
        profiles,
        snapshots,
        clock,
        script,
        native_home,
    )

    mismatched_private = coordinator.associate_account(
        dual_before.account_id,
        expected_identity=association_b,
    )
    mismatched_state = (
        _failure_kind(mismatched_private),
        store.read_saved(dual_before.account_id),
        snapshots.load(dual_before),
    )

    script.set_status(native_config, status_a)
    changed_native = coordinator.associate_account(
        dual_before.account_id,
        expected_identity=association_b,
    )
    changed_state = (
        _failure_kind(changed_native),
        store.read_saved(dual_before.account_id),
        snapshots.load(dual_before),
    )

    script.set_status(native_config, status_b)
    migrated = coordinator.associate_account(
        dual_before.account_id,
        expected_identity=association_b,
    )

    assert isinstance(migrated, CredentialLoginSuccess)
    assert (mismatched_state, changed_state) == (
        (
            ProviderFailureKind.IDENTITY_MISMATCH,
            dual_before,
            usage_before,
        ),
        (
            ProviderFailureKind.IDENTITY_MISMATCH,
            dual_before,
            usage_before,
        ),
    )
    current_a = store.read_saved(original_a.account_id)
    current_b = store.read_saved(dual_before.account_id)
    assert current_a == original_a
    assert current_b is not None
    current_authority = current_b.authority
    assert isinstance(current_authority, ClaudeAccountAuthority)
    managed = current_authority.subscription
    assert isinstance(managed, ClaudeManagedLoginAuthority)
    assert current_authority.setup_token == setup_authority_b
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
        provider_identity=association_b,
    )
    assert {account.label for account in store.saved_accounts()} == {
        AccountLabel("mismatch"),
        AccountLabel("dual"),
    }
    assert legacy_a.is_dir()
    assert setup_b.is_dir()
    assert (
        repository.read_payload(
            dual_before.account_id,
            setup_authority_b.authority_id,
        )
        == setup_payload_before
    )
    assert (
        profile_a.exists(),
        script.login_profiles,
        script.interactive_profiles,
        native_sentinel.read_bytes(),
    ) == (
        False,
        [profile_b, profile_b],
        [profile_b, profile_b],
        b"native-login-must-remain",
    )

    assert (
        coordinator.restore_setup_only(
            dual_before.account_id,
            expected_identity=association_b,
        ),
        store.read_saved(dual_before.account_id),
        snapshots.load(dual_before),
    ) == (
        CredentialLoginSuccess(dual_before.label),
        replace(
            current_b,
            authority=ClaudeAccountAuthority(
                setup_token=setup_authority_b,
                subscription=None,
            ),
            credential_health=setup_authority_b.health,
            last_refresh_at=None,
            last_refresh_status=None,
            last_refresh_error_code=None,
        ),
        usage_before,
    )


def test_interrupted_setup_association_recovers_profile_and_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both association commit gaps preserve setup authority and metrics."""
    use_synthetic_claude(monkeypatch)
    paths = make_application_paths(tmp_path)
    store, private = make_account_store_with_private(
        tmp_path,
        (_setup_account("recover", "recover"),),
    )
    original = store.saved_accounts()[0]
    setup_authority = _setup_authority(original)
    repository = CredentialAuthorityRepository(private)
    setup_bundle = repository.bundle_path(
        original.account_id,
        setup_authority.authority_id,
    )
    setup_payload = _protected_payload(
        repository,
        original,
        setup_authority.authority_id,
    )
    index_payload = paths.accounts.read_bytes()
    snapshots = UsageSnapshotStore(paths.usage_snapshots)
    usage_before = snapshots.save(_usage_snapshot(original))
    profiles = profile_tree(paths)
    profile = managed_profile(
        paths,
        original.account_id,
    ).config_directory
    status, _association = claude_profile_status("recover")
    script = ClaudeManagedLoginScript(
        profiles,
        {
            profile: (
                credential_payload(
                    None,
                    None,
                    token_suffix="recover-new",
                    access_expires_at=_NEW_ACCESS_EXPIRY,
                ),
                credential_payload(
                    None,
                    None,
                    token_suffix="recover-resumed",
                    access_expires_at=_NEW_ACCESS_EXPIRY + timedelta(hours=1),
                ),
            )
        },
        interactive_payloads={
            profile: credential_payload(
                None,
                None,
                token_suffix="recover-browser",
                access_expires_at=_INTERACTIVE_ACCESS_EXPIRY,
            )
        },
        profile_statuses={profile: status},
    )
    native_sentinel = _native_sentinel(tmp_path / "native-claude")
    coordinator = _coordinator(
        paths,
        store,
        private,
        profiles,
        snapshots,
        FixedClock(),
        script,
        native_sentinel.parent,
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
        coordinator.migrate_account(
            original.account_id,
            establish_identity=True,
            interactive=True,
        )

    assert (
        paths.accounts.read_bytes(),
        store.read_saved(original.account_id),
        _protected_payload(
            repository,
            original,
            setup_authority.authority_id,
        ),
    ) == (
        index_payload,
        original,
        setup_payload,
    )
    assert profiles.read_owned_file(
        profile,
        CLAUDE_CREDENTIAL_FILE,
    ) is not None
    _assert_pending_usage(snapshots, original, usage_before)
    monkeypatch.setattr(store, "migrate_stored_authority", commit)

    promote_identity = snapshots.promote_identity

    def stop_before_usage_promotion(
        account_id: SidekickAccountId,
        provider_id: ProviderId,
        provider_identity: ProviderIdentity,
    ) -> None:
        del account_id, provider_id, provider_identity
        raise _SimulatedCrash

    monkeypatch.setattr(
        snapshots,
        "promote_identity",
        stop_before_usage_promotion,
    )
    with pytest.raises(_SimulatedCrash):
        coordinator.migrate_account(
            original.account_id,
            establish_identity=True,
            interactive=True,
        )

    current, _ = _managed_account(
        store,
        original,
        setup_authority,
    )
    assert (
        repository.read_payload(
            current.account_id,
            setup_authority.authority_id,
        ),
        setup_bundle.is_dir(),
        script.login_profiles,
        script.interactive_profiles,
    ) == (setup_payload, True, [profile, profile], [profile])
    expected_usage = _assert_pending_usage(snapshots, current, usage_before)

    monkeypatch.setattr(snapshots, "promote_identity", promote_identity)
    recovered = coordinator.migrate_account(
        original.account_id,
        establish_identity=False,
        interactive=False,
    )

    assert (
        isinstance(recovered, CredentialLoginSuccess),
        snapshots.pending_identity_promotions(ProviderId.CLAUDE),
        snapshots.load(current),
        script.login_profiles,
        native_sentinel.read_bytes(),
    ) == (
        True,
        (),
        expected_usage,
        [profile, profile],
        b"native-login-must-remain",
    )

    _, before_subscription = _managed_account(
        store,
        original,
        setup_authority,
    )
    script.set_authority(
        profile,
        credential_payload(
            None,
            None,
            token_suffix="recover-provider-ahead",
            access_expires_at=_NEW_ACCESS_EXPIRY + timedelta(hours=2),
        ),
        status,
    )

    provider_ahead = coordinator.migrate_account(
        original.account_id,
        establish_identity=False,
        interactive=False,
    )

    after_provider_ahead, after_subscription = _managed_account(
        store,
        original,
        setup_authority,
    )
    assert isinstance(provider_ahead, CredentialLoginSuccess), provider_ahead
    assert (
        after_provider_ahead.credential_health,
        after_subscription.generation != before_subscription.generation,
        script.login_profiles,
        native_sentinel.read_bytes(),
    ) == (
        CredentialHealth.HEALTHY,
        True,
        [profile, profile],
        b"native-login-must-remain",
    )
