"""Managed Claude credential maintenance and authority tests."""

import sys
from pathlib import Path

import pytest

from sidekick_usages.core.models import ClaudeLoginCredentials
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    require_managed_claude_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.entrypoints.usage_lookup import (
    managed_credential_factories,
)
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from tests.fakes.claude.maintenance import (
    ACCOUNT_A,
    ACCOUNT_B,
    MANAGED_LOGIN_ENVIRONMENT_KEYS,
    NATIVE_LOGIN_ENVIRONMENT_KEYS,
    PRIVATE_PROCESS_UMASK,
    claude_login_profile,
    execute_due_maintenance,
    maintenance_scenario,
    managed_login_records,
    resolver_scenario,
    unverified_generation_scenario,
)
from tests.support.platform import REQUIRES_MANAGED_RUNTIME


@REQUIRES_MANAGED_RUNTIME
def test_managed_resolver_uses_selected_native_and_inactive_private_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One logical account opens exactly one verified Claude authority."""
    scenario = resolver_scenario(tmp_path, monkeypatch)
    factories = managed_credential_factories(
        scenario.paths,
        PersistenceService(
            scenario.paths,
            maintenance_quiescent=lambda: True,
        ),
        scenario.store,
        scenario.clock,
        scenario.environment,
        claude_runtime=scenario.runtime,
    )
    lease_factory = factories[ProviderId.CLAUDE]
    tokens: list[str] = []
    for account in (
        scenario.selected_account,
        scenario.inactive_account,
    ):
        with (
            OperationAuthorityLock(
                scenario.paths.durable_operations,
                account.account_id,
            ).hold() as authority,
            lease_factory(account, authority) as authenticated,
        ):
            credentials = authenticated.lease.account.credentials
            assert isinstance(credentials, ClaudeLoginCredentials)
            tokens.append(credentials.access_token)

    assert tokens == [
        "sk-ant-oat01-account-a-native",
        "sk-ant-oat01-account-b-private",
    ]
    assert (
        sum(
            arguments == ("--version",)
            for _path, arguments in scenario.runner.calls
        )
        == 1
    )


@REQUIRES_MANAGED_RUNTIME
def test_managed_claude_maintenance_isolated_per_account_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = maintenance_scenario(tmp_path, monkeypatch)

    assert execute_due_maintenance(scenario) == (
        (ACCOUNT_A, WorkerOutcome.TRANSIENT_FAILURE),
        (ACCOUNT_B, WorkerOutcome.SUCCEEDED),
    )

    selected = SelectedStateStore(scenario.paths.selected_state).load(
        ProviderId.CLAUDE
    )
    assert selected is not None
    assert selected.account_id == scenario.selected_before.account_id
    assert (
        selected.provider_identity
        == scenario.selected_before.provider_identity
    )
    assert (
        selected.runtime_generation
        != scenario.selected_before.runtime_generation
    )

    records = managed_login_records(scenario.runner)
    profiles = (
        scenario.profile_a,
        scenario.profile_b,
        scenario.native_profile.config_directory,
    )
    assert tuple(record.executable for record in records) == (
        (Path(sys.executable).resolve(),) * len(profiles)
    )
    assert (
        tuple(claude_login_profile(record.environment) for record in records)
        == profiles
    )
    for record, expected_token in zip(
        records,
        (
            "refresh-account-a-old",
            "refresh-account-b-old",
            "refresh-account-b-old",
        ),
        strict=True,
    ):
        expected_keys = (
            MANAGED_LOGIN_ENVIRONMENT_KEYS
            if "CLAUDE_CONFIG_DIR" in record.environment
            else NATIVE_LOGIN_ENVIRONMENT_KEYS
        )
        assert record.environment.keys() == expected_keys
        assert (
            record.environment["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"]
            == expected_token
        )
        assert (
            record.environment["CLAUDE_CODE_OAUTH_SCOPES"]
            == "user:profile user:inference"
        )
        assert not set(scenario.unsafe_parent.values()) & set(
            record.environment.values()
        )
        assert record.working_directory == claude_login_profile(
            record.environment
        )
        assert (
            record.timeout_seconds,
            record.maximum_output_bytes,
            record.umask,
        ) == (
            60.0,
            1024 * 1024,
            PRIVATE_PROCESS_UMASK,
        )

    saved = {
        account.account_id: account
        for account in scenario.store.saved_accounts()
    }
    original = {account.account_id: account for account in scenario.original}
    assert saved[ACCOUNT_A].authority == original[ACCOUNT_A].authority
    assert (
        saved[ACCOUNT_A].credential_health
        is original[ACCOUNT_A].credential_health
    )
    assert saved[ACCOUNT_A].last_refresh_status is RefreshStatus.FAILED
    assert saved[ACCOUNT_B].last_refresh_status is RefreshStatus.OK
    private = require_managed_claude_authority(saved[ACCOUNT_B])
    previous = require_managed_claude_authority(original[ACCOUNT_B])
    assert private.provider_identity == previous.provider_identity
    assert private.generation != previous.generation
    assert private.generation != selected.runtime_generation

    protected_a = scenario.profiles.read_relative_authority_file(
        str(ACCOUNT_A),
        CLAUDE_CREDENTIAL_FILE,
    )
    protected_b = scenario.profiles.read_relative_authority_file(
        str(ACCOUNT_B),
        CLAUDE_CREDENTIAL_FILE,
    )
    assert protected_a is not None
    assert protected_b is not None
    assert protected_a.data == scenario.payload_a
    assert protected_b.data == scenario.refreshed_private_b
    assert scenario.native_file.read_bytes() == scenario.refreshed_native_b
    persisted = scenario.paths.accounts.read_bytes()
    for secret in (
        b"account-a-old",
        b"account-b-old",
        b"account-b-private-new",
        b"account-b-native-new",
        b"parent-secret",
    ):
        assert secret not in persisted


def test_managed_claude_maintenance_rejects_unverified_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = unverified_generation_scenario(tmp_path, monkeypatch)

    result = scenario.coordinator.refresh(ACCOUNT_A)

    saved = scenario.store.read_saved(ACCOUNT_A)
    assert saved is not None
    assert result.outcome is ClaudeManagedOutcome.UNCHANGED
    assert saved.authority == scenario.original.authority
    assert saved.last_refresh_status is RefreshStatus.FAILED
    protected = scenario.profiles.read_relative_authority_file(
        str(ACCOUNT_A),
        CLAUDE_CREDENTIAL_FILE,
    )
    assert protected is not None
    assert protected.data == scenario.payload
    rendered = repr(result) + scenario.paths.accounts.read_text()
    assert "unchanged-generation" not in rendered
    assert "claude_managed_unchanged" in rendered
