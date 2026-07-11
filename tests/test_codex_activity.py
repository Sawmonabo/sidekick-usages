"""Behavioral tests for Codex account token activity."""

from collections.abc import Mapping

import pytest

from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    TokenActivitySummary,
)
from sidekick_usages.core.types import (
    AccountLabel,
    TokenActivityScope,
)
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.activity import (
    ACTIVITY_URL,
    CodexActivity,
)
from sidekick_usages.providers.codex.schemas import parse_activity_response
from sidekick_usages.serialization import JsonObject


class CapturingHttp(HttpClient):
    """Capture one JSON GET without constructing a transport pool."""

    def __init__(self, payload: JsonObject) -> None:
        self.payload = payload
        self.request: tuple[str, dict[str, str]] | None = None

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        self.request = url, dict(headers)
        return self.payload


class RejectingHttp(CapturingHttp):
    """Reject the profile as an expired saved credential."""

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        self.request = url, dict(headers)
        raise AuthError("Token expired or invalid (HTTP 401).")


def _account() -> Account:
    return Account(
        label=AccountLabel("codex-account"),
        credentials=CodexCredentials(
            access_token="test-only-access",
            refresh_token="test-only-refresh",
            expiry=UnknownExpiry(),
            account_id="acct_test",
        ),
        plan="pro",
    )


def test_profile_uses_exact_account_route_and_authoritative_lifetime() -> None:
    http = CapturingHttp(
        {
            "stats": {
                "lifetime_tokens": 7_449_473_297,
                "peak_daily_tokens": 749_395_781,
                "longest_running_turn_sec": 23_463,
                "current_streak_days": 1,
                "longest_streak_days": 29,
                "daily_usage_buckets": [
                    {"start_date": "2026-07-09", "tokens": 3},
                    {"start_date": "2026-07-10", "tokens": 4},
                ],
            }
        }
    )

    result = CodexActivity().read(_account(), http)

    assert result == TokenActivitySummary(
        total_tokens=7_449_473_297,
        scope=TokenActivityScope.ACCOUNT,
    )
    assert http.request is not None
    url, headers = http.request
    assert url == ACTIVITY_URL
    assert headers["Authorization"] == "Bearer test-only-access"
    assert headers["ChatGPT-Account-Id"] == "acct_test"
    assert headers["User-Agent"] == "codex-cli"


@pytest.mark.parametrize(
    ("stats", "expected_kind"),
    [
        ({}, ProviderFailureKind.INCOMPLETE),
        ({"lifetime_tokens": True}, ProviderFailureKind.MALFORMED),
        ({"lifetime_tokens": -1}, ProviderFailureKind.MALFORMED),
        (
            {"lifetime_tokens": 9_223_372_036_854_775_808},
            ProviderFailureKind.MALFORMED,
        ),
        (
            {
                "lifetime_tokens": 1,
                "daily_usage_buckets": [
                    {"start_date": "not-a-date", "tokens": 1}
                ],
            },
            ProviderFailureKind.MALFORMED,
        ),
    ],
)
def test_profile_rejects_values_that_cannot_be_lifetime_activity(
    stats: JsonObject,
    expected_kind: ProviderFailureKind,
) -> None:
    with pytest.raises(ProviderBoundaryError) as captured:
        parse_activity_response({"stats": stats})
    assert captured.value.failure.kind is expected_kind


def test_authentication_failure_never_becomes_a_local_number() -> None:
    http = RejectingHttp({})

    with pytest.raises(AuthError):
        CodexActivity().read(_account(), http)

    assert http.request is not None
