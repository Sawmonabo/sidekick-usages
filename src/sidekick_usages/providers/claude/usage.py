"""Claude usage routes, scope policy, and response conversion."""

from typing import assert_never

from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    UsageReport,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.providers.claude.credentials import (
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schema.usage import (
    header_usage_window,
    oauth_usage_windows,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
USER_AGENT = "claude-code/2.1.174"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_API_VERSION = "2023-06-01"
PROBE_MODEL = "claude-haiku-4-5-20251001"

HEADER_BUCKETS: tuple[tuple[str, str], ...] = (
    ("anthropic-ratelimit-unified-5h", "5h"),
    ("anthropic-ratelimit-unified-7d", "7d"),
)


def fetch_usage(account: Account, http: HttpClient) -> UsageReport:
    """Fetch usage through the route owned by the credential variant."""
    credentials = require_claude_credentials(account)
    match credentials:
        case ClaudeSetupTokenCredentials():
            return fetch_via_headers(account, http)
        case ClaudeLoginCredentials():
            return fetch_via_oauth_endpoint(account, http)
        case unexpected:
            assert_never(unexpected)


def fetch_via_oauth_endpoint(
    account: Account,
    http: HttpClient,
) -> UsageReport:
    """Fetch and convert the full-scope OAuth usage response."""
    data = http.get_json(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {account.access_token}",
            "User-Agent": USER_AGENT,
            "anthropic-beta": ANTHROPIC_BETA,
        },
    )
    windows = oauth_usage_windows(data)
    return UsageReport(windows=windows, plan=account.plan)


def fetch_via_headers(
    account: Account,
    http: HttpClient,
) -> UsageReport:
    """Probe messages and convert unified rate-limit response headers."""
    response_headers = http.post_capture_headers(
        MESSAGES_URL,
        {
            "model": PROBE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "quota"}],
        },
        {
            "Authorization": f"Bearer {account.access_token}",
            "anthropic-version": ANTHROPIC_API_VERSION,
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
        },
        operation=HttpOperation.CLAUDE_PROBE,
    )
    windows = tuple(
        window
        for prefix, label in HEADER_BUCKETS
        if (window := header_usage_window(prefix, label, response_headers))
        is not None
    )
    return UsageReport(windows=windows, plan=account.plan)
