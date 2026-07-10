"""Claude usage routes, scope policy, and response conversion."""

from sidekick_usages.core.models import Account, UsageReport
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.claude.credentials import (
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schemas import (
    header_usage_window,
    oauth_usage_window,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
USER_AGENT = "claude-code/2.1.174"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_API_VERSION = "2023-06-01"
PROFILE_SCOPE = "user:profile"
PROBE_MODEL = "claude-haiku-4-5-20251001"

OAUTH_BUCKETS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_oauth_apps", "7d OAuth"),
)
HEADER_BUCKETS: tuple[tuple[str, str], ...] = (
    ("anthropic-ratelimit-unified-5h", "5h"),
    ("anthropic-ratelimit-unified-7d", "7d"),
)


def fetch_usage(account: Account, http: HttpClient) -> UsageReport:
    """Fetch usage through the route selected by known scope metadata."""
    credentials = require_claude_credentials(account)
    if (
        credentials.scopes is not None
        and PROFILE_SCOPE not in credentials.scopes
    ):
        return fetch_via_headers(account, http)
    return fetch_via_oauth_endpoint(account, http)


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
    windows = tuple(
        window
        for key, label in OAUTH_BUCKETS
        if (window := oauth_usage_window(data.get(key), label)) is not None
    )
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
