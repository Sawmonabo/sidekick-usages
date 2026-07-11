"""Codex usage requests and validated response conversion."""

from sidekick_usages.core.models import Account, UsageReport
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.codex.request import account_headers
from sidekick_usages.providers.codex.schemas import parse_usage_response

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"


def fetch_usage(account: Account, http: HttpClient) -> UsageReport:
    """Fetch and validate Codex usage for one saved account."""
    headers = account_headers(account)
    headers.update(
        {
            "Accept": "application/json",
            "OpenAI-Beta": "codex",
        }
    )
    data = http.get_json(
        USAGE_URL,
        headers=headers,
    )
    return parse_usage_response(data)


__all__ = ["fetch_usage"]
