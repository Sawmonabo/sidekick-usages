"""Codex account token-activity requests."""

from sidekick_usages.core.models import TokenActivityReading
from sidekick_usages.core.types import ProviderId
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import (
    ProviderAuthenticatedAccount,
    runtime_account,
)
from sidekick_usages.providers.codex.request import account_headers
from sidekick_usages.providers.codex.schemas import parse_activity_response

ACTIVITY_URL = "https://chatgpt.com/backend-api/wham/profiles/me"


class CodexActivity:
    """Read authoritative lifetime activity for one saved Codex account."""

    provider_id = ProviderId.CODEX

    def read(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
    ) -> TokenActivityReading:
        """Fetch and validate one account-scoped activity profile."""
        headers = account_headers(runtime_account(account))
        headers["Accept"] = "application/json"
        return parse_activity_response(
            http.get_json(ACTIVITY_URL, headers=headers)
        )


__all__ = ["ACTIVITY_URL", "CodexActivity"]
