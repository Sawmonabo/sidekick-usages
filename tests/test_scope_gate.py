"""Tests for OAuth-scope extraction and persistence.

The CLI no longer skips fetching usage when scopes look
inference-only — see :mod:`test_header_path` for the new routing
that fetches via response headers instead. What these tests still
pin is the upstream of that decision: the scope field must round-trip
correctly through every layer so the routing has clean inputs.

* :func:`sidekick_usages.providers.claude.ClaudeProvider._parse_blob`
  surfaces a valid ``scopes`` array as
  :attr:`DetectedCredentials.scopes` and tolerates the field being
  missing or malformed.
* :class:`sidekick_usages.store.Account` round-trips ``scopes``
  through :meth:`to_dict` / :meth:`from_dict`, including legacy
  accounts persisted before the field existed.
"""

from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.store import Account


def _make_acct(scopes: list[str] | None) -> Account:
    """Build a minimal Account fixture for round-trip tests.

    :param scopes: Value to assign to :attr:`Account.scopes`.
    :return: Account with sentinel fields and the given scopes.
    """
    return Account(
        label="t",
        provider_id="claude",
        access_token="sk-ant-oat01-x",
        scopes=scopes,
    )


def test_parse_blob_extracts_scopes_list() -> None:
    """Scopes from the local creds file land on DetectedCredentials."""
    blob = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-abc",
            "scopes": [
                "user:file_upload",
                "user:inference",
                "user:mcp_servers",
                "user:profile",
                "user:sessions:claude_code",
            ],
            "subscriptionType": "max",
        }
    }
    detected = ClaudeProvider._parse_blob(blob)
    assert detected is not None
    assert detected.scopes == [
        "user:file_upload",
        "user:inference",
        "user:mcp_servers",
        "user:profile",
        "user:sessions:claude_code",
    ]


def test_parse_blob_missing_scopes_yields_none() -> None:
    """Older creds without ``scopes`` produce ``scopes=None``."""
    blob = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}}
    detected = ClaudeProvider._parse_blob(blob)
    assert detected is not None
    assert detected.scopes is None


def test_parse_blob_rejects_malformed_scopes() -> None:
    """Junk in ``scopes`` is treated as unknown, not crashed-on.

    Defensive: future creds-file shape drift must not break detect.
    """
    blob = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-abc",
            "scopes": "not-a-list",
        }
    }
    detected = ClaudeProvider._parse_blob(blob)
    assert detected is not None
    assert detected.scopes is None


def test_parse_blob_rejects_mixed_type_scopes() -> None:
    """Mixed-type ``scopes`` are rejected wholesale (not partial)."""
    blob = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-abc",
            "scopes": ["user:inference", 42, None],
        }
    }
    detected = ClaudeProvider._parse_blob(blob)
    assert detected is not None
    assert detected.scopes is None


def test_account_roundtrips_scopes() -> None:
    """``scopes`` survives ``to_dict`` → ``from_dict``."""
    original = _make_acct(["user:inference", "user:profile"])
    restored = Account.from_dict(original.label, original.to_dict())
    assert restored.scopes == ["user:inference", "user:profile"]


def test_account_from_dict_tolerates_missing_scopes_field() -> None:
    """Accounts persisted before the scopes field load as None."""
    legacy = {
        "provider_id": "claude",
        "access_token": "sk-ant-oat01-x",
        "plan": "max",
    }
    restored = Account.from_dict("legacy", legacy)
    assert restored.scopes is None


def test_account_roundtrips_provider_account_id() -> None:
    """Provider account identity survives persistence for Codex."""
    original = Account(
        label="codex-pro",
        provider_id="codex",
        access_token="eyJ.access.sig",
        provider_account_id="acct_123",
    )

    restored = Account.from_dict(original.label, original.to_dict())

    assert restored.provider_account_id == "acct_123"


def test_account_roundtrips_codex_auth_home_metadata() -> None:
    """Codex per-account auth homes survive persistence."""
    original = Account(
        label="codex-pro",
        provider_id="codex",
        access_token="eyJ.access.sig",
        provider_account_id="acct_123",
        refresh_token="refresh-123",
        codex_home="/home/me/.codex-pro",
        codex_id_token="id-token-123",
        codex_last_refresh="2026-06-12T00:00:00Z",
    )

    restored = Account.from_dict(original.label, original.to_dict())

    assert restored.codex_home == "/home/me/.codex-pro"
    assert restored.codex_id_token == "id-token-123"
    assert restored.codex_last_refresh == "2026-06-12T00:00:00Z"
