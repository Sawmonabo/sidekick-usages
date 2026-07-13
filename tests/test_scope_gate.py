"""Focused provider-metadata and compatibility-store tests."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import InvalidExpiry, KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    RefreshStatus,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.providers.base import ProviderBoundaryError
from sidekick_usages.providers.claude.credential_schemas import (
    claude_expiry,
    parse_credentials_blob,
)
from sidekick_usages.providers.codex.schemas import jwt_expiry
from sidekick_usages.serialization import JsonValue, decode_json_object
from tests.test_support import make_account_store


def test_claude_parser_preserves_known_scope_order() -> None:
    """A valid provider scope list remains ordered and immutable."""
    detected = parse_credentials_blob(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abc",
                "refreshToken": "refresh-abc",
                "expiresAt": 1_800_000_000_000,
                "scopes": ["user:inference", "user:profile"],
            }
        }
    )

    assert isinstance(detected.credentials, ClaudeLoginCredentials)
    assert detected.scopes == ("user:inference", "user:profile")


def test_claude_parser_rejects_absent_login_scopes() -> None:
    """Native login state cannot impersonate explicit setup-token input."""
    with pytest.raises(ProviderBoundaryError):
        parse_credentials_blob(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-abc",
                    "refreshToken": "refresh-abc",
                    "expiresAt": 1_800_000_000_000,
                }
            }
        )


@pytest.mark.parametrize("scopes", ["not-a-list", ["user:inference", 42]])
def test_claude_parser_rejects_malformed_scopes(scopes: JsonValue) -> None:
    """Malformed scope metadata cannot become partially trusted state."""
    with pytest.raises(ProviderBoundaryError):
        parse_credentials_blob(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-abc",
                    "refreshToken": "refresh-abc",
                    "expiresAt": 1_800_000_000_000,
                    "scopes": scopes,
                }
            }
        )


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode())
    return f"e30.{encoded.decode().rstrip('=')}.sig"


def test_provider_native_expiry_units_converge_and_fail_closed() -> None:
    """Provider-native epochs converge and malformed values stay invalid."""
    expected = KnownExpiry(
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=1_800_000_000)
    )

    assert claude_expiry(1_800_000_000_000) == expected
    assert jwt_expiry(_jwt({"exp": 1_800_000_000})) == expected
    assert isinstance(claude_expiry(True), InvalidExpiry)
    with pytest.raises(ProviderBoundaryError):
        jwt_expiry(_jwt({"exp": True}))


def test_store_round_trips_exact_provider_state(tmp_path: Path) -> None:
    """The compatibility codec preserves exact units and aware state."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    codex_expiry_seconds = 1_900_000_000
    codex_expiry = epoch + timedelta(seconds=codex_expiry_seconds)
    eastern = timezone(timedelta(hours=-4))
    audit_time = datetime(2026, 6, 12, 8, 34, 56, 789000, tzinfo=eastern)
    reset_time = datetime(2026, 6, 12, 9, tzinfo=eastern)
    audit_time_utc = audit_time.astimezone(UTC)
    reset_time_utc = reset_time.astimezone(UTC)
    claude_account = Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-x",
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
    store = make_account_store(tmp_path, (claude_account, codex_account))

    raw = decode_json_object(store.path.read_bytes())
    records = raw["accounts"]
    assert isinstance(records, dict)
    claude_record = records["claude-team"]
    codex_record = records["codex-pro"]
    assert isinstance(claude_record, dict)
    assert isinstance(codex_record, dict)
    assert claude_record["credential_kind"] == "setup_token"
    assert "access_expires_at" not in claude_record
    assert "scopes" not in claude_record
    assert codex_record["expires_at"] == "2030-03-17T17:46:40.000000Z"
    assert claude_record["last_refresh_at"] == "2026-06-12T12:34:56.789000Z"
    assert (
        claude_record["heartbeat_5h_reset_at"] == "2026-06-12T13:00:00.000000Z"
    )

    restored = make_account_store(tmp_path)
    claude = restored.get("claude-team")
    codex = restored.get("codex-pro")

    assert claude is not None
    assert isinstance(claude.credentials, ClaudeSetupTokenCredentials)
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

    assert isinstance(codex_account.credentials, CodexCredentials)
    codex_account.credentials = replace(
        codex_account.credentials,
        expiry=InvalidExpiry(),
    )
    with pytest.raises(InvalidSchemaError):
        store.persist(codex_account)
    with pytest.raises(ValueError, match="Account labels"):
        restored.rename("claude-team", "invalid\x00label")
