"""Generation-one Claude classification and migration preflight tests."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.migrations.credential_kinds import (
    CredentialMigrationPreflightError,
    LegacyClaudeCredentialKind,
    classify_version_one,
    require_migratable_version_one,
)
from sidekick_usages.persistence.schemas import (
    StoredAccountRecord,
    StoredClaudeIdentity,
    VersionOneDocument,
)

_EXPIRY = datetime(2026, 7, 12, 12, tzinfo=UTC)
_SETUP_RECORD_COUNT = 2


def _claude(
    label: str,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    scopes: tuple[str, ...] | None = None,
) -> StoredAccountRecord:
    return StoredAccountRecord(
        label=AccountLabel(label),
        provider_id=ProviderId.CLAUDE,
        provider_account_id=None,
        access_token=access_token or f"test-only-{label}-access",
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan="team",
        scopes=scopes,
        codex_home=None,
        codex_id_token=None,
        codex_last_refresh=None,
        last_refresh_at=None,
        last_refresh_status=None,
        last_refresh_error=None,
        heartbeat_enabled=False,
        heartbeat_5h_reset_at=None,
        heartbeat_window_resets=None,
        heartbeat_targets=None,
        last_heartbeat_at=None,
        last_heartbeat_status=None,
        last_heartbeat_error=None,
    )


def test_classifier_totally_identifies_setup_and_login_records() -> None:
    """Every deterministic generation-one Claude shape gets one kind."""
    document = VersionOneDocument(
        (
            _claude("setup-none"),
            _claude("setup-inference", scopes=("user:inference",)),
            _claude(
                "login",
                refresh_token="test-only-login-refresh",
                expires_at=_EXPIRY,
                scopes=("user:inference", "user:profile"),
            ),
        )
    )

    result = classify_version_one(document)

    assert tuple(item.kind for item in result.claude_records) == (
        LegacyClaudeCredentialKind.SETUP_TOKEN,
        LegacyClaudeCredentialKind.SETUP_TOKEN,
        LegacyClaudeCredentialKind.SUBSCRIPTION_LOGIN,
    )
    assert result.setup_count == _SETUP_RECORD_COUNT
    assert result.login_count == 1
    assert result.refresh_expiry_unavailable_count == 1
    assert result.issues == ()


@pytest.mark.parametrize(
    ("refresh_token", "expires_at", "scopes", "with_identity"),
    [
        ("test-only-refresh", None, ("user:profile",), False),
        (None, _EXPIRY, ("user:profile",), False),
        (None, None, ("user:profile",), False),
        ("test-only-refresh", _EXPIRY, ("user:inference",), False),
        (None, None, None, True),
    ],
)
def test_classifier_blocks_every_ambiguous_legacy_shape(
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: tuple[str, ...] | None,
    *,
    with_identity: bool,
) -> None:
    """Partial login signals are reported by label without guessing."""
    record = _claude(
        "needs-repair",
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )
    if with_identity:
        record = replace(
            record,
            claude_identity=StoredClaudeIdentity(
                "test-only-account",
                "test-only-organization",
            ),
        )

    result = classify_version_one(VersionOneDocument((record,)))

    assert result.claude_records[0].kind is (
        LegacyClaudeCredentialKind.AMBIGUOUS
    )
    assert result.issues[0].labels == ("needs-repair",)
    assert result.issues[0].next_commands == (
        ("sidekick-usages", "remove", "needs-repair"),
        ("sidekick-usages", "migrate", "accounts"),
    )


@pytest.mark.parametrize("field_name", ["access_token", "refresh_token"])
def test_classifier_rejects_duplicate_provider_credential_ownership(
    field_name: str,
) -> None:
    """Duplicate diagnostics identify labels but never derive token text."""
    first = _claude(
        "first",
        access_token="test-only-first-access",
        refresh_token="test-only-first-refresh",
        expires_at=_EXPIRY,
        scopes=("user:profile",),
    )
    second = replace(
        first,
        label=AccountLabel("second"),
        access_token="test-only-second-access",
        refresh_token="test-only-second-refresh",
    )
    second = replace(second, **{field_name: getattr(first, field_name)})

    result = classify_version_one(VersionOneDocument((first, second)))

    assert result.issues[0].labels == ("first", "second")
    with pytest.raises(CredentialMigrationPreflightError) as exc_info:
        require_migratable_version_one(VersionOneDocument((first, second)))
    representation = repr(exc_info.value)
    assert getattr(first, field_name) not in representation
    assert "sha256" not in representation.lower()
