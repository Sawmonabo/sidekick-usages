"""Tests for Codex auth, refresh, and usage parsing."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sidekick_usages.http import HttpClient
from sidekick_usages.providers.codex import CodexProvider
from sidekick_usages.store import Account

DETECTED_EXP = 1_800_000_000
REFRESH_EXP = 1_900_000_000
PRIMARY_USED = 12
SECONDARY_USED = 34
EXTRA_PRIMARY_USED = 56
EXTRA_SECONDARY_USED = 78
PRIMARY_RESET = 1_770_003_600
SECONDARY_RESET = 1_770_604_800
EXTRA_PRIMARY_RESET = 1_770_007_200
EXTRA_SECONDARY_RESET = 1_770_691_200


def _jwt(payload: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped fixture for parser tests."""
    header = {"alg": "none", "typ": "JWT"}

    def enc(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc(header)}.{enc(payload)}.sig"


def _acct() -> Account:
    """Build a Codex account fixture."""
    acct = Account(
        label="codex-pro",
        provider_id="codex",
        access_token="access-old",
        refresh_token="refresh-old",
        expires_at=1_700_000_000,
        plan="unknown",
    )
    acct.provider_account_id = "acct_123"
    return acct


class _UsageHttp(HttpClient):
    """HTTP fake that records GET headers and returns one payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.headers: dict[str, str] | None = None

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        del url
        self.headers = headers
        return self.payload


class _RefreshHttp(HttpClient):
    """HTTP fake that records POST form data and returns one payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.data: dict[str, str] | None = None

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del url, headers
        self.data = data
        return self.payload


def _usage_payload() -> dict[str, Any]:
    """Return the current Codex usage endpoint shape."""
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": PRIMARY_USED,
                "reset_at": PRIMARY_RESET,
            },
            "secondary_window": {
                "used_percent": SECONDARY_USED,
                "reset_at": SECONDARY_RESET,
            },
        },
        "additional_rate_limits": [
            {
                "limit_name": "gpt-5.1-codex",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": EXTRA_PRIMARY_USED,
                        "reset_at": EXTRA_PRIMARY_RESET,
                    },
                    "secondary_window": {
                        "used_percent": EXTRA_SECONDARY_USED,
                        "reset_at": EXTRA_SECONDARY_RESET,
                    },
                },
            }
        ],
    }


def test_parse_blob_extracts_account_id_expiry_and_plan() -> None:
    """Codex auth.json supplies the account binding and JWT metadata."""
    access = _jwt(
        {
            "exp": DETECTED_EXP,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "pro",
                "chatgpt_account_id": "acct_from_claim",
            },
        }
    )
    detected = CodexProvider._parse_blob(
        {
            "tokens": {
                "access_token": access,
                "refresh_token": "refresh-123",
                "account_id": "acct_from_tokens",
            }
        }
    )

    assert detected is not None
    assert detected.access_token == access
    assert detected.refresh_token == "refresh-123"
    assert detected.provider_account_id == "acct_from_tokens"
    assert detected.expires_at == DETECTED_EXP
    assert detected.plan == "pro"


def test_parse_blob_preserves_codex_auth_file_metadata() -> None:
    """Codex auth metadata is needed to write isolated CODEX_HOME files."""
    access = _jwt(
        {
            "exp": DETECTED_EXP,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "pro",
                "chatgpt_account_id": "acct_from_claim",
            },
        }
    )

    detected = CodexProvider._parse_blob(
        {
            "last_refresh": "2026-06-12T00:00:00Z",
            "tokens": {
                "access_token": access,
                "refresh_token": "refresh-123",
                "id_token": "id-token-123",
            },
        }
    )

    assert detected is not None
    assert detected.id_token == "id-token-123"
    assert detected.last_refresh == "2026-06-12T00:00:00Z"


def test_detect_credentials_reads_explicit_codex_home(tmp_path: Path) -> None:
    """A saved account can point at its own CODEX_HOME."""
    codex_home = tmp_path / "codex-a"
    codex_home.mkdir()
    access = _jwt(
        {
            "exp": DETECTED_EXP,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct_home",
                "chatgpt_plan_type": "pro",
            },
        }
    )
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "last_refresh": "2026-06-12T00:00:00Z",
                "tokens": {
                    "access_token": access,
                    "refresh_token": "refresh-home",
                    "id_token": "id-token-home",
                },
            }
        )
    )

    detected = CodexProvider().detect_credentials(codex_home)

    assert detected is not None
    assert detected.access_token == access
    assert detected.refresh_token == "refresh-home"
    assert detected.provider_account_id == "acct_home"
    assert detected.id_token == "id-token-home"
    assert detected.last_refresh == "2026-06-12T00:00:00Z"


def test_fetch_usage_sends_codex_account_and_beta_headers() -> None:
    """Codex usage requires both account id and OpenAI-Beta headers."""
    http = _UsageHttp(_usage_payload())

    CodexProvider().fetch_usage(_acct(), http)

    assert http.headers is not None
    assert http.headers["ChatGPT-Account-Id"] == "acct_123"
    assert http.headers["OpenAI-Beta"] == "codex"


def test_fetch_usage_parses_current_rate_limit_shape() -> None:
    """Current Codex payload renders 5h, 7d, and additional windows."""
    report = CodexProvider().fetch_usage(
        _acct(),
        _UsageHttp(_usage_payload()),
    )

    by_name = {window.name: window for window in report.windows}
    assert report.plan == "pro"
    assert by_name["5h"].utilization == PRIMARY_USED
    assert by_name["7d"].utilization == SECONDARY_USED
    assert by_name["gpt-5.1-codex 5h"].utilization == EXTRA_PRIMARY_USED
    assert by_name["gpt-5.1-codex 7d"].utilization == EXTRA_SECONDARY_USED
    assert (
        by_name["5h"].resets_at
        == datetime.fromtimestamp(
            PRIMARY_RESET,
            tz=UTC,
        ).isoformat()
    )


def test_refresh_posts_codex_client_id_and_updates_metadata() -> None:
    """Codex refresh uses the installed CLI client id and rotates tokens."""
    access = _jwt(
        {
            "exp": REFRESH_EXP,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct_new",
                "chatgpt_plan_type": "pro",
            },
        }
    )
    http = _RefreshHttp(
        {
            "access_token": access,
            "refresh_token": "refresh-new",
        }
    )
    acct = _acct()

    assert CodexProvider().refresh_token(acct, http) is True

    assert http.data == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    }
    assert acct.access_token == access
    assert acct.refresh_token == "refresh-new"
    assert acct.expires_at == REFRESH_EXP
    assert acct.provider_account_id == "acct_new"


def test_refresh_writes_rotated_tokens_to_saved_codex_home(tmp_path) -> None:
    """Refresh rotation is persisted to the account's isolated auth.json."""
    codex_home = tmp_path / "codex-pro"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-06-11T00:00:00Z",
                "tokens": {
                    "access_token": "access-old",
                    "refresh_token": "refresh-old",
                    "id_token": "id-old",
                    "account_id": "acct_123",
                },
            }
        )
    )
    access = _jwt(
        {
            "exp": REFRESH_EXP,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct_new",
                "chatgpt_plan_type": "pro",
            },
        }
    )
    http = _RefreshHttp(
        {
            "access_token": access,
            "refresh_token": "refresh-new",
            "id_token": "id-new",
        }
    )
    acct = _acct()
    acct.codex_home = str(codex_home)

    assert CodexProvider().refresh_token(acct, http) is True

    saved_auth = json.loads((codex_home / "auth.json").read_text())
    assert saved_auth["auth_mode"] == "chatgpt"
    assert saved_auth["tokens"]["access_token"] == access
    assert saved_auth["tokens"]["refresh_token"] == "refresh-new"
    assert saved_auth["tokens"]["id_token"] == "id-new"
    assert saved_auth["tokens"]["account_id"] == "acct_new"
    assert saved_auth["last_refresh"] != "2026-06-11T00:00:00Z"
