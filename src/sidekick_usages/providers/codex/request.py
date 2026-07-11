"""Shared authenticated ChatGPT request metadata for Codex adapters."""

from dataclasses import replace

from sidekick_usages.core.models import Account
from sidekick_usages.errors import ProviderIdentityError
from sidekick_usages.providers.codex.auth import require_codex_credentials
from sidekick_usages.providers.codex.schemas import account_id_from_token

CODEX_USER_AGENT = "codex-cli"


def account_id(account: Account) -> str:
    """Return or derive the saved account's ChatGPT identity."""
    credentials = require_codex_credentials(account)
    resolved = credentials.account_id
    if resolved is None:
        resolved = account_id_from_token(account.access_token)
        if resolved is not None:
            account.credentials = replace(credentials, account_id=resolved)
    if resolved is None:
        raise ProviderIdentityError(
            "Missing Codex account id. Log in to Codex again, then "
            f"sidekick-usages refresh {account.label}."
        )
    return resolved


def account_headers(account: Account) -> dict[str, str]:
    """Build the common authenticated headers for one saved account."""
    return {
        "Authorization": f"Bearer {account.access_token}",
        "ChatGPT-Account-Id": account_id(account),
        "User-Agent": CODEX_USER_AGENT,
    }


__all__ = ["CODEX_USER_AGENT", "account_headers", "account_id"]
