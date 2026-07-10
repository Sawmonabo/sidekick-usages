"""Focused provider-metadata and compatibility-store tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import InvalidExpiry, KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.errors import UsageError
from sidekick_usages.providers.claude import ClaudeProvider, _claude_expiry
from sidekick_usages.providers.codex import _jwt_expiry
from sidekick_usages.serialization import JsonValue, decode_json_object
from sidekick_usages.store import AccountStore
from tests.test_support import make_application_paths


def test_claude_parser_preserves_known_scope_order() -> None:
    """A valid provider scope list remains ordered and immutable."""
    detected = ClaudeProvider._parse_blob(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abc",
                "scopes": ["user:inference", "user:profile"],
            }
        }
    )

    assert detected is not None
    assert detected.scopes == ("user:inference", "user:profile")


@pytest.mark.parametrize(
    "scopes",
    [None, "not-a-list", ["user:inference", 42]],
)
def test_claude_parser_treats_absent_or_malformed_scopes_as_unknown(
    scopes: JsonValue,
) -> None:
    """Malformed scope metadata never becomes a partially trusted list."""
    oauth: dict[str, JsonValue] = {"accessToken": "sk-ant-oat01-abc"}
    if scopes is not None:
        oauth["scopes"] = scopes

    detected = ClaudeProvider._parse_blob({"claudeAiOauth": oauth})

    assert detected is not None
    assert detected.scopes is None


@pytest.mark.parametrize(
    ("provider_id", "native_value", "is_valid"),
    [
        (ProviderId.CLAUDE, 1_800_000_000_000, True),
        (ProviderId.CODEX, 1_800_000_000, True),
        (ProviderId.CLAUDE, True, False),
        (ProviderId.CODEX, True, False),
        (ProviderId.CLAUDE, -1, False),
        (ProviderId.CODEX, -1, False),
        (ProviderId.CLAUDE, "invalid", False),
        (ProviderId.CODEX, "invalid", False),
    ],
)
def test_provider_native_expiry_units_converge_and_fail_closed(
    provider_id: ProviderId,
    native_value: JsonValue,
    *,
    is_valid: bool,
) -> None:
    """Provider epochs converge; malformed values remain explicitly invalid."""
    expiry = (
        _claude_expiry(native_value)
        if provider_id is ProviderId.CLAUDE
        else _jwt_expiry({"exp": native_value})
    )

    if is_valid:
        assert expiry == KnownExpiry(
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=1_800_000_000)
        )
    else:
        assert isinstance(expiry, InvalidExpiry)


def test_store_round_trips_exact_provider_state(tmp_path: Path) -> None:
    """The compatibility codec preserves exact units and aware state."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    claude_expiry_ms = 1_800_000_000_123
    codex_expiry_seconds = 1_900_000_000
    claude_expiry = epoch + timedelta(milliseconds=claude_expiry_ms)
    codex_expiry = epoch + timedelta(seconds=codex_expiry_seconds)
    eastern = timezone(timedelta(hours=-4))
    audit_time = datetime(2026, 6, 12, 8, 34, 56, 789000, tzinfo=eastern)
    reset_time = datetime(2026, 6, 12, 9, tzinfo=eastern)
    audit_time_utc = audit_time.astimezone(UTC)
    reset_time_utc = reset_time.astimezone(UTC)
    store = AccountStore(make_application_paths(tmp_path).accounts)
    claude_account = Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeCredentials(
            access_token="sk-ant-oat01-x",
            expiry=KnownExpiry(claude_expiry),
            scopes=("user:inference", "user:profile"),
        ),
        last_refresh_at=audit_time,
        last_refresh_status=RefreshStatus.OK,
        heartbeat_5h_reset_at=reset_time,
        heartbeat_window_resets={"standard": reset_time},
    )
    codex_account = Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="eyJ.access.sig",
            refresh_token="refresh-123",
            expiry=KnownExpiry(codex_expiry),
            account_id="acct_123",
            auth_home="/synthetic/codex-pro",
            id_token="id-token-123",
            auth_last_refresh="2026-06-12T00:00:00Z",
        ),
        last_heartbeat_at=audit_time,
        last_heartbeat_status=HeartbeatStatus.WARMED,
    )
    store.upsert(claude_account)
    store.upsert(codex_account)
    store.save()

    raw = decode_json_object(store.path.read_bytes())
    claude_record = raw["claude-team"]
    codex_record = raw["codex-pro"]
    assert isinstance(claude_record, dict)
    assert isinstance(codex_record, dict)
    assert claude_record["expires_at"] == claude_expiry_ms
    assert codex_record["expires_at"] == codex_expiry_seconds
    assert claude_record["last_refresh_at"] == "2026-06-12T12:34:56.789000Z"
    assert claude_record["heartbeat_5h_reset_at"] == "2026-06-12T13:00:00Z"

    restored = AccountStore(make_application_paths(tmp_path).accounts).load()
    claude = restored.get("claude-team")
    codex = restored.get("codex-pro")

    assert claude is not None
    assert claude.expiry == KnownExpiry(claude_expiry)
    assert claude.scopes == ("user:inference", "user:profile")
    assert claude.last_refresh_at == audit_time_utc
    assert claude.heartbeat_5h_reset_at == reset_time_utc
    assert claude.heartbeat_window_resets == {"standard": reset_time_utc}
    assert codex is not None
    assert codex.expiry == KnownExpiry(codex_expiry)
    assert codex.provider_account_id == "acct_123"
    assert codex.codex_home == "/synthetic/codex-pro"
    assert codex.codex_id_token == "id-token-123"
    assert codex.codex_last_refresh == "2026-06-12T00:00:00Z"
    assert codex.last_heartbeat_at == audit_time_utc

    assert isinstance(claude_account.credentials, ClaudeCredentials)
    claude_account.credentials = replace(
        claude_account.credentials,
        expiry=KnownExpiry(claude_expiry + timedelta(microseconds=1)),
    )
    with pytest.raises(UsageError, match="precision"):
        store.save()
    with pytest.raises(UsageError, match="Account labels"):
        restored.rename("claude-team", "invalid\x00label")
