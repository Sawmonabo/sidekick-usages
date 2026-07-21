"""Current account transforms and released rollback contracts."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence import transforms
from sidekick_usages.persistence.migrations.credential_kinds import (
    CredentialMigrationPreflightError,
)
from sidekick_usages.persistence.schemas import (
    StoredAccountRecord,
    VersionOneDocument,
    decode_generation_zero,
    decode_version_two,
    encode_generation_zero,
    encode_version_two,
)

_ACCESS_EXPIRY = datetime(2026, 7, 12, 12, tzinfo=UTC)
_REFRESH_EXPIRY = datetime(2026, 12, 1, tzinfo=UTC)


def _rich_accounts() -> tuple[Account, ...]:
    return (
        Account(
            label=AccountLabel("claude-setup"),
            credentials=ClaudeSetupTokenCredentials(
                access_token="test-only-setup-access"
            ),
            plan="team",
            heartbeat_enabled=True,
        ),
        Account(
            label=AccountLabel("claude-login"),
            credentials=ClaudeLoginCredentials(
                access_token="test-only-login-access",
                refresh_token="test-only-login-refresh",
                access_expiry=KnownExpiry(_ACCESS_EXPIRY),
                refresh_expiry=KnownExpiry(_REFRESH_EXPIRY),
                scopes=("user:inference", "user:profile"),
                identity=ClaudeLoginIdentity(
                    account_id="test-only-account-id",
                    organization_id="test-only-organization-id",
                ),
            ),
            plan="max",
            last_refresh_at=_ACCESS_EXPIRY,
        ),
        Account(
            label=AccountLabel("codex-plus"),
            credentials=CodexCredentials(
                access_token="test-only-codex-access",
                refresh_token="test-only-codex-refresh",
                expiry=KnownExpiry(_ACCESS_EXPIRY),
                account_id="test-only-codex-account",
                auth_home="/synthetic/codex",
                id_token="test-only-codex-id",
            ),
            plan="plus",
        ),
    )


def _legacy_claude(
    label: str,
    *,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: tuple[str, ...] | None,
) -> StoredAccountRecord:
    return StoredAccountRecord(
        label=AccountLabel(label),
        provider_id=ProviderId.CLAUDE,
        provider_account_id=None,
        access_token=f"test-only-{label}-access",
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan="team",
        scopes=scopes,
        codex_home=None,
        codex_id_token=None,
        codex_last_refresh=None,
        last_refresh_at=_ACCESS_EXPIRY,
        last_refresh_status=None,
        last_refresh_error=None,
        heartbeat_enabled=True,
        heartbeat_5h_reset_at=None,
        heartbeat_window_resets=None,
        heartbeat_targets=("standard",),
        last_heartbeat_at=None,
        last_heartbeat_status=None,
        last_heartbeat_error=None,
    )


def test_current_runtime_round_trip_preserves_rich_claude_metadata() -> None:
    """Current persistence represents every closed runtime variant."""
    accounts = _rich_accounts()

    document = transforms.accounts_to_version_two(accounts)
    payload = encode_version_two(document)
    decoded = decode_version_two(payload)

    assert transforms.version_two_to_accounts(decoded) == accounts
    representation = repr(decoded)
    for secret in (
        "setup-access",
        "login-access",
        "login-refresh",
        "account-id",
        "organization-id",
        "codex-access",
        "codex-refresh",
        "codex-id",
    ):
        assert secret not in representation


def test_version_one_migration_is_deterministic_and_preserves_order() -> None:
    """Legacy setup/login state reconstructs without kind inference later."""
    source = VersionOneDocument(
        (
            _legacy_claude(
                "setup",
                refresh_token=None,
                expires_at=None,
                scopes=("user:inference",),
            ),
            _legacy_claude(
                "login",
                refresh_token="test-only-login-refresh",
                expires_at=_ACCESS_EXPIRY,
                scopes=("user:profile",),
            ),
        )
    )

    current = transforms.version_one_to_version_two(source)
    accounts = transforms.version_two_to_accounts(current)

    assert tuple(str(account.label) for account in accounts) == (
        "setup",
        "login",
    )
    assert accounts[0].credentials == ClaudeSetupTokenCredentials(
        access_token="test-only-setup-access"
    )
    assert accounts[1].credentials == ClaudeLoginCredentials(
        access_token="test-only-login-access",
        refresh_token="test-only-login-refresh",
        access_expiry=KnownExpiry(_ACCESS_EXPIRY),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
        identity=None,
    )
    assert accounts[0].heartbeat_targets == ("standard",)


def test_ambiguous_version_one_migration_fails_before_transform() -> None:
    """The forward transform cannot guess from partial login signals."""
    source = VersionOneDocument(
        (
            _legacy_claude(
                "needs-repair",
                refresh_token="test-only-refresh",
                expires_at=None,
                scopes=("user:profile",),
            ),
        )
    )

    with pytest.raises(CredentialMigrationPreflightError):
        transforms.version_one_to_version_two(source)


def test_released_round_trip_loses_only_advisory_login_metadata() -> None:
    """v0.6.0 preserves every secret-bearing and operational account field."""
    current = transforms.accounts_to_version_two(_rich_accounts())

    released = transforms.version_two_to_v060(current)
    payload = encode_generation_zero(released)
    reconstructed = transforms.generation_zero_to_version_two(
        decode_generation_zero(payload)
    )
    accounts = transforms.version_two_to_accounts(reconstructed)

    expected = list(_rich_accounts())
    login = expected[1]
    assert isinstance(login.credentials, ClaudeLoginCredentials)
    expected[1] = replace(
        login,
        credentials=replace(
            login.credentials,
            refresh_expiry=UnknownExpiry(),
            identity=None,
        ),
    )
    assert accounts == tuple(expected)
    released_json = json.loads(payload)
    assert released_json["claude-setup"]["scopes"] == ["user:inference"]
