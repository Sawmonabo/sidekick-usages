"""Codex usage requests and validated response conversion."""

from dataclasses import replace

from sidekick_usages.core.models import Account, UsageReport
from sidekick_usages.errors import UsageError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.codex.auth import require_codex_credentials
from sidekick_usages.providers.codex.schemas import (
    account_id_from_token,
    parse_usage_response,
)

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
USER_AGENT = "codex-cli/0.139.0"


def fetch_usage(account: Account, http: HttpClient) -> UsageReport:
    """Fetch and validate Codex usage for one saved account."""
    credentials = require_codex_credentials(account)
    account_id = credentials.account_id
    if account_id is None:
        account_id = account_id_from_token(account.access_token)
        if account_id is not None:
            account.credentials = replace(
                credentials,
                account_id=account_id,
            )
    if account_id is None:
        raise UsageError(
            "Missing Codex account id. Log in to Codex again, then "
            f"sidekick-usages refresh {account.label}."
        )
    data = http.get_json(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {account.access_token}",
            "ChatGPT-Account-Id": account_id,
            "OpenAI-Beta": "codex",
            "User-Agent": USER_AGENT,
        },
    )
    return parse_usage_response(data)


__all__ = ["USER_AGENT", "fetch_usage"]
