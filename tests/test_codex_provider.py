"""Load-bearing Codex auth parsing and usage tests."""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.http.client import HttpClient
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.providers.codex.schemas import auth_blob_account_id
from sidekick_usages.serialization.json import JsonObject

REFRESH_EXP = 1_900_000_000
PRIMARY_RESET = 1_770_003_600
PRIMARY_USED = 12
EXTRA_SECONDARY_USED = 78


def _jwt(payload: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped provider fixture."""
    header = {"alg": "none", "typ": "JWT"}

    def encode(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.sig"


def _access_token(
    *,
    account_id: str = "acct_123",
    expiry: int = REFRESH_EXP,
    plan: str = "pro",
) -> str:
    return _jwt(
        {
            "exp": expiry,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan,
            },
        }
    )


def _account() -> Account:
    return Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="access-old",
            refresh_token="refresh-old",
            expiry=KnownExpiry(datetime.fromtimestamp(1_700_000_000, UTC)),
            account_id="acct_123",
        ),
        plan="unknown",
    )


class _UsageHttp(HttpClient):
    """Record GET headers and return one usage payload."""

    def __init__(self, payload: JsonObject) -> None:
        self.payload = payload
        self.headers: dict[str, str] | None = None

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        del url
        self.headers = dict(headers)
        return self.payload


def _usage_payload() -> JsonObject:
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": PRIMARY_USED,
                "reset_at": PRIMARY_RESET,
            },
            "secondary_window": {
                "used_percent": 34,
                "reset_at": 1_770_604_800,
            },
        },
        "additional_rate_limits": [
            {
                "limit_name": "gpt-5.1-codex",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 56,
                        "reset_at": 1_770_007_200,
                    },
                    "secondary_window": {
                        "used_percent": EXTRA_SECONDARY_USED,
                        "reset_at": 1_770_691_200,
                    },
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("declared_id", "claim_id", "expected"),
    [
        ("acct_123", "acct_123", "acct_123"),
        ("acct_123", None, "acct_123"),
        (None, "acct_123", "acct_123"),
        (
            "acct_declared",
            "acct_claimed",
            ProviderFailureKind.IDENTITY_MISMATCH,
        ),
    ],
)
def test_auth_identity_resolution_never_prefers_a_conflict(
    declared_id: str | None,
    claim_id: str | None,
    expected: str | ProviderFailureKind,
) -> None:
    tokens: JsonObject = {}
    if declared_id is not None:
        tokens["account_id"] = declared_id
    if claim_id is not None:
        tokens["access_token"] = _access_token(account_id=claim_id)
    blob: JsonObject = {"tokens": tokens}

    if isinstance(expected, ProviderFailureKind):
        with pytest.raises(ProviderBoundaryError) as exc_info:
            auth_blob_account_id(blob)
        assert exc_info.value.failure.kind is expected
        return
    assert auth_blob_account_id(blob) == expected


def test_usage_validates_current_shape_and_required_headers() -> None:
    """Current usage becomes normalized windows with account-bound headers."""
    http = _UsageHttp(_usage_payload())

    report = CodexProvider().validate_credentials(_account(), http)

    assert http.headers is not None
    assert http.headers["ChatGPT-Account-Id"] == "acct_123"
    assert http.headers["OpenAI-Beta"] == "codex"
    by_name = {window.name: window for window in report.windows}
    assert report.plan == "pro"
    assert by_name["5h"].utilization == PRIMARY_USED
    assert by_name["gpt-5.1-codex 7d"].utilization == EXTRA_SECONDARY_USED
    assert by_name["5h"].resets_at == datetime.fromtimestamp(
        PRIMARY_RESET,
        tz=UTC,
    )


def test_usage_rejects_malformed_window_without_exposing_input() -> None:
    raw_secret = "raw-token-body-should-never-escape"
    payload = _usage_payload()
    rate_limit = payload["rate_limit"]
    assert isinstance(rate_limit, dict)
    primary = rate_limit["primary_window"]
    assert isinstance(primary, dict)
    primary["used_percent"] = raw_secret

    with pytest.raises(ProviderBoundaryError) as caught:
        CodexProvider().validate_credentials(_account(), _UsageHttp(payload))

    assert caught.value.failure.kind is ProviderFailureKind.MALFORMED
    assert caught.value.failure.fields == (
        "rate_limit.primary_window.used_percent",
    )
    assert raw_secret not in repr(caught.value)
    assert raw_secret not in repr(caught.value.failure)
