"""Claude credential maintenance and legacy refresh behavior tests."""

import os
import sys
from collections.abc import Mapping
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
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.selection.types import OperationKind
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    CLAUDE_CREDENTIAL_FILE,
    ClaudeManagedAuthorityReader,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    require_managed_claude_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.service import (
    ClaudeManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.daemon.lifecycle.readiness import SupervisorReadiness
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.claude.maintenance import (
    ClaudeManagedMaintenanceWorkerExecutor,
)
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
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
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
    RefreshSuccess,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import ClaudeCommandResult
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.claude.managed import (
    CLAUDE_LOGGED_IN_STATUS,
    CLAUDE_LOGGED_OUT_STATUS,
    CLAUDE_LOGIN_HELP_OUTPUT,
    CLAUDE_VERSION_OUTPUT,
    ClaudeRunner,
    credential_payload,
    managed_capabilities,
    managed_profile,
    profile_tree,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    authenticated_account,
    make_application_paths,
)

_ACCOUNT_A = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_ACCOUNT_B = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_AUTHORITY_A = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_AUTHORITY_B = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_INITIAL_EXPIRY = REFERENCE_TIME + timedelta(minutes=10)
_FUTURE_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_MANAGED_LOGIN_ENVIRONMENT_KEYS = frozenset(
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
_PRIVATE_PROCESS_UMASK = 0o077 if os.name == "posix" else -1


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
    setup_token: bool = False,
    scopes: tuple[str, ...] = ("user:profile",),
) -> Account:
    credentials = (
        ClaudeSetupTokenCredentials(access_token="sk-ant-oat01-old")
        if setup_token
        else ClaudeLoginCredentials(
            access_token="sk-ant-oat01-old",
            refresh_token="refresh-old",
            access_expiry=KnownExpiry(_FUTURE_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=scopes,
        )
    )
    return Account(
        label=AccountLabel("claude-team"),
        credentials=credentials,
    )


def _credentials(result: RefreshSuccess) -> ClaudeLoginCredentials:
    credentials = result.credentials
    assert isinstance(credentials, ClaudeLoginCredentials)
    return credentials


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
            managed_capabilities(
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


def _execute_due_managed_maintenance(
    paths: ApplicationPaths,
    coordinator: ClaudeManagedAuthorityCoordinator,
    clock: FixedClock,
) -> tuple[WorkerOutcome, ...]:
    SupervisorReadiness(paths, clock).enroll_accounts()
    operations = tuple(
        operation
        for operation in OperationQueueStore(paths.durable_operations).due(
            clock.now()
        )
        if operation.kind is OperationKind.MAINTAIN
    )
    executor = ClaudeManagedMaintenanceWorkerExecutor(coordinator, clock)
    outcomes: list[WorkerOutcome] = []
    for operation in operations:
        with OperationAuthorityLock(
            paths.durable_operations,
            operation.required_account_id,
        ).hold() as authority:
            outcomes.append(executor.execute(operation, authority).outcome)
    return tuple(outcomes)


def _assert_managed_login_boundaries(
    runner: ClaudeRunner,
    profiles: tuple[Path, Path],
    expected_tokens: tuple[str, str],
    unsafe_parent: dict[str, str],
) -> None:
    records = tuple(
        (path, environment, working_directory, timeout, limit, umask)
        for (
            (path, arguments),
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
        )
        if arguments == ("auth", "login", "--claudeai")
    )
    assert tuple(record[0] for record in records) == (
        Path(sys.executable).resolve(),
        Path(sys.executable).resolve(),
    )
    assert (
        tuple(
            Path(record[1]["CLAUDE_CONFIG_DIR"])
            for record in records
            if record[1] is not None
        )
        == profiles
    )
    for record, expected_token in zip(
        records,
        expected_tokens,
        strict=True,
    ):
        environment = record[1]
        assert environment is not None
        assert environment.keys() == _MANAGED_LOGIN_ENVIRONMENT_KEYS
        assert environment["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == expected_token
        assert (
            environment["CLAUDE_CODE_OAUTH_SCOPES"]
            == "user:profile user:inference"
        )
        assert not set(unsafe_parent.values()) & set(environment.values())
        assert record[2] == Path(environment["CLAUDE_CONFIG_DIR"])
        assert record[3:] == (
            60.0,
            1024 * 1024,
            _PRIVATE_PROCESS_UMASK,
        )


def test_refresh_missing_token_is_explicit_and_does_not_mutate() -> None:
    account = _account(setup_token=True)
    original = account.credentials

    result = _provider().refresh_credentials(
        authenticated_account(account),
        _FakeHttp(),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MISSING
    assert result.cause is ProviderFailureCause.MISSING_REFRESH_CREDENTIAL
    assert account.credentials is original


def test_managed_claude_maintenance_isolated_per_account_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_a = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="account-a-old",
        access_expires_at=_INITIAL_EXPIRY,
    )
    payload_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="account-b-old",
        access_expires_at=_INITIAL_EXPIRY,
    )
    refreshed_b = credential_payload(
        "provider-account-b",
        "provider-organization-b",
        token_suffix="account-b-new",
        access_expires_at=_FUTURE_EXPIRY,
    )
    paths, store, profiles, original = _seed_managed_accounts(
        tmp_path / "state",
        (
            (_ACCOUNT_A, _AUTHORITY_A, AccountLabel("claude-a"), payload_a),
            (_ACCOUNT_B, _AUTHORITY_B, AccountLabel("claude-b"), payload_b),
        ),
    )
    profile_a = managed_profile(paths, _ACCOUNT_A).config_directory
    profile_b = managed_profile(paths, _ACCOUNT_B).config_directory
    native_profile = tmp_path / "native"
    native_profile.mkdir()
    native_sentinel = native_profile / CLAUDE_CREDENTIAL_FILE
    native_sentinel.write_bytes(b"native-login-must-remain")
    unsafe_parent = {
        "ANTHROPIC_API_KEY": "parent-api-secret",
        "ANTHROPIC_AUTH_TOKEN": "parent-auth-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth-secret",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "parent-refresh-secret",
        "CLAUDE_CODE_OAUTH_SCOPES": "parent:scope",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CONFIG_DIR": str(native_profile),
        "SIDEKICK_UNRELATED_SECRET": "unrelated-parent-secret",
    }
    source_environment = {
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "USER": "sidekick-test",
        **unsafe_parent,
    }
    final_profiles = {profile_a, profile_b}

    def script(
        arguments: tuple[str, ...],
        environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        del working_directory
        if arguments == ("--version",):
            return ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT)
        if arguments == ("auth", "login", "--help"):
            return ClaudeCommandResult(0, CLAUDE_LOGIN_HELP_OUTPUT)
        if arguments == ("auth", "status"):
            assert environment is not None
            return (
                ClaudeCommandResult(0, CLAUDE_LOGGED_IN_STATUS)
                if Path(environment["CLAUDE_CONFIG_DIR"]) in final_profiles
                else ClaudeCommandResult(1, CLAUDE_LOGGED_OUT_STATUS)
            )
        if arguments != ("auth", "login", "--claudeai"):
            raise AssertionError(f"Unexpected Claude command: {arguments!r}")
        assert environment is not None
        config_directory = Path(environment["CLAUDE_CONFIG_DIR"])
        if config_directory == profile_a:
            return ClaudeCommandResult(
                1,
                b"failed child-output-secret-a",
            )
        assert config_directory == profile_b
        profiles.write_owned_file(
            profile_b,
            CLAUDE_CREDENTIAL_FILE,
            refreshed_b,
        )
        return ClaudeCommandResult(0, b"child-output-secret-b")

    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )
    runner = ClaudeRunner(script=script)
    clock = FixedClock()
    coordinator = ClaudeManagedAuthorityCoordinator(
        paths,
        store,
        profiles,
        clock,
        environment=source_environment,
        host=HostPlatform.LINUX,
        runner=runner,
    )
    outcomes = _execute_due_managed_maintenance(paths, coordinator, clock)

    assert outcomes == (
        WorkerOutcome.TRANSIENT_FAILURE,
        WorkerOutcome.SUCCEEDED,
    )
    _assert_managed_login_boundaries(
        runner,
        (profile_a, profile_b),
        ("refresh-account-a-old", "refresh-account-b-old"),
        unsafe_parent,
    )
    saved = {account.account_id: account for account in store.saved_accounts()}
    protected_a = profiles.read_relative_authority_file(
        str(_ACCOUNT_A),
        CLAUDE_CREDENTIAL_FILE,
    )
    protected_b = profiles.read_relative_authority_file(
        str(_ACCOUNT_B),
        CLAUDE_CREDENTIAL_FILE,
    )
    assert protected_a is not None
    assert protected_b is not None
    assert protected_a.data == payload_a
    assert protected_b.data == refreshed_b
    assert saved[_ACCOUNT_A].authority == original[0].authority
    assert saved[_ACCOUNT_A].credential_health is original[0].credential_health
    assert saved[_ACCOUNT_A].last_refresh_status is RefreshStatus.FAILED
    assert saved[_ACCOUNT_B].last_refresh_status is RefreshStatus.OK
    assert (
        require_managed_claude_authority(saved[_ACCOUNT_B]).provider_identity
        == require_managed_claude_authority(original[1]).provider_identity
    )
    assert (
        require_managed_claude_authority(saved[_ACCOUNT_B]).generation
        != require_managed_claude_authority(original[1]).generation
    )
    assert native_sentinel.read_bytes() == b"native-login-must-remain"
    persisted = paths.accounts.read_bytes()
    for secret in (
        b"account-a-old",
        b"account-b-old",
        b"account-b-new",
        b"child-output-secret",
        b"parent-secret",
    ):
        assert secret not in persisted


@pytest.mark.parametrize(
    ("scopes", "expected_scope"),
    [
        (("user:profile",), "user:profile"),
        (
            ("user:inference", "user:profile"),
            "user:inference user:profile",
        ),
    ],
)
def test_http_refresh_preserves_scope_state_and_returns_new_credentials(
    scopes: tuple[str, ...],
    expected_scope: str,
) -> None:
    account = _account(scopes=scopes)
    original = account.credentials
    http = _FakeHttp(
        {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "refresh-new",
            "expires_in": 60,
        }
    )

    result = _provider().refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, RefreshSuccess)
    refreshed = _credentials(result)
    assert refreshed.access_token == "sk-ant-oat01-new"
    assert refreshed.refresh_token == "refresh-new"
    assert refreshed.access_expiry == KnownExpiry(
        REFERENCE_TIME + timedelta(seconds=60)
    )
    assert refreshed.scopes == scopes
    assert http.body is not None
    assert http.body["scope"] == expected_scope
    assert account.credentials is original


def test_refresh_rejection_is_typed_and_secret_safe() -> None:
    account = _account()
    original = account.credentials
    raw_secret = "sk-ant-oat01-rejected-secret"

    result = _provider().refresh_credentials(
        authenticated_account(account),
        _FakeHttp(failure=AuthError(raw_secret)),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert result.cause is ProviderFailureCause.PROVIDER_REJECTED_REFRESH
    assert result.message == "Claude rejected the saved subscription login."
    assert "log in again" not in result.message.lower()
    assert raw_secret not in repr(result)
    assert account.credentials is original


def test_transient_refresh_failure_is_a_cause_without_recovery_copy() -> None:
    result = _provider().refresh_credentials(
        authenticated_account(_account()),
        _FakeHttp(failure=TransientError("raw provider detail")),
    )

    assert isinstance(result, ProviderFailure)
    assert result.cause is (
        ProviderFailureCause.REFRESH_TEMPORARILY_UNAVAILABLE
    )
    assert result.message == "Claude refresh is temporarily unavailable."
    assert "raw provider detail" not in repr(result)
    assert "log in again" not in result.message.lower()


def test_managed_claude_maintenance_rejects_unverified_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = credential_payload(
        "provider-account-a",
        "provider-organization-a",
        token_suffix="unchanged-generation",
        access_expires_at=_INITIAL_EXPIRY,
    )
    paths, store, profiles, original = _seed_managed_accounts(
        tmp_path / "state",
        (
            (
                _ACCOUNT_A,
                _AUTHORITY_A,
                AccountLabel("claude-a"),
                payload,
            ),
        ),
    )
    profile = managed_profile(paths, _ACCOUNT_A).config_directory

    def script(
        arguments: tuple[str, ...],
        environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        del working_directory
        if arguments == ("--version",):
            return ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT)
        if arguments == ("auth", "login", "--help"):
            return ClaudeCommandResult(0, CLAUDE_LOGIN_HELP_OUTPUT)
        if arguments == ("auth", "status"):
            assert environment is not None
            return (
                ClaudeCommandResult(0, CLAUDE_LOGGED_IN_STATUS)
                if Path(environment["CLAUDE_CONFIG_DIR"]) == profile
                else ClaudeCommandResult(1, CLAUDE_LOGGED_OUT_STATUS)
            )
        if arguments == ("auth", "login", "--claudeai"):
            return ClaudeCommandResult(
                0,
                b"sk-ant-oat01-child-output-secret",
            )
        raise AssertionError(f"Unexpected Claude command: {arguments!r}")

    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )
    result = ClaudeManagedAuthorityCoordinator(
        paths,
        store,
        profiles,
        FixedClock(),
        environment={"PATH": os.environ["PATH"], "USER": "sidekick-test"},
        host=HostPlatform.LINUX,
        runner=ClaudeRunner(script=script),
    ).refresh(_ACCOUNT_A)

    saved = store.read_saved(_ACCOUNT_A)
    assert saved is not None
    assert result.outcome is ClaudeManagedOutcome.UNCHANGED
    assert saved.authority == original[0].authority
    assert saved.last_refresh_status is RefreshStatus.FAILED
    protected = profiles.read_relative_authority_file(
        str(_ACCOUNT_A),
        CLAUDE_CREDENTIAL_FILE,
    )
    assert protected is not None
    assert protected.data == payload
    rendered = repr(result) + paths.accounts.read_text()
    assert "child-output-secret" not in rendered
    assert "unchanged-generation" not in rendered
    assert "claude_managed_unchanged" in rendered


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ({"refresh_token": "refresh-new"}, ProviderFailureKind.INCOMPLETE),
        (
            {
                "access_token": "sk-ant-oat01-new",
                "refresh_token": "",
                "expires_in": 60,
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
    response: JsonObject,
    kind: ProviderFailureKind,
) -> None:
    account = _account()
    original = account.credentials
    raw_identity = "long.account.name@example.test"
    response["provider_identity"] = raw_identity

    with pytest.raises(ProviderBoundaryError) as exc_info:
        _provider().refresh_credentials(
            authenticated_account(account),
            _FakeHttp(response),
        )

    assert exc_info.value.failure.kind is kind
    assert exc_info.value.failure.cause is (
        ProviderFailureCause.REFRESH_OUTPUT_INCOMPLETE
        if kind is ProviderFailureKind.INCOMPLETE
        else ProviderFailureCause.REFRESH_OUTPUT_MALFORMED
    )
    rendered = repr(exc_info.value.failure)
    assert raw_identity not in rendered
    assert "sk-ant-oat01-new" not in rendered
    assert account.credentials is original


def test_expired_login_credential_fails_before_provider_contact() -> None:
    account = Account(
        label=AccountLabel("expired-login"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-current",
            refresh_token="refresh-expired",
            access_expiry=KnownExpiry(_FUTURE_EXPIRY),
            refresh_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
            scopes=("user:profile",),
        ),
    )
    http = _FakeHttp({"access_token": "sk-ant-oat01-unused"})

    result = _provider().refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, ProviderFailure)
    assert result.cause is ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED
    assert result.message == "The saved Claude login credential has expired."
    assert http.body is None
