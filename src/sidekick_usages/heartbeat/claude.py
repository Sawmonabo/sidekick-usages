"""Claude-specific usage-window heartbeat adapter."""

from datetime import UTC, datetime

from sidekick_usages.heartbeat.base import HeartbeatProvider, warmed
from sidekick_usages.heartbeat.domain import (
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.claude import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_BETA,
    MESSAGES_URL,
    PROBE_MODEL,
    PROFILE_SCOPE,
    USAGE_URL,
    USER_AGENT,
)
from sidekick_usages.store import Account

INFERENCE_SCOPE = "user:inference"
FIVE_HOUR_KEY = "five_hour"
FIVE_HOUR_HEADER_PREFIX = "anthropic-ratelimit-unified-5h"


class ClaudeHeartbeat(HeartbeatProvider):
    """Window warming for Claude OAuth and setup-token accounts."""

    id = "claude"
    display_name = "Claude Code"

    def supports(self, account: Account) -> bool:
        """Claude can warm when inference access is known or unknown."""
        if account.provider_id != self.id:
            return False
        if account.scopes is None:
            return True
        if PROFILE_SCOPE in account.scopes:
            return INFERENCE_SCOPE in account.scopes
        return True

    def unsupported_message(self, account: Account) -> str:
        """Return the missing-scope detail for Claude accounts."""
        if account.scopes is not None and PROFILE_SCOPE in account.scopes:
            return (
                "Claude heartbeat requires user:inference scope to send "
                "the tiny warming request."
            )
        return super().unsupported_message(account)

    def inspect_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        """Read Claude's usage window when the OAuth usage route is available."""
        del target
        if account.scopes is not None and PROFILE_SCOPE not in account.scopes:
            return UsageWindowState(
                active=False, message="header probe needed"
            )

        data = http.get_json(
            USAGE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {account.access_token}",
                "User-Agent": USER_AGENT,
                "anthropic-beta": ANTHROPIC_BETA,
            },
        )
        window = data.get(FIVE_HOUR_KEY)
        if not isinstance(window, dict):
            return UsageWindowState(active=False, message="5h window missing")
        reset_at = window.get("resets_at")
        if isinstance(reset_at, str) and reset_at:
            return UsageWindowState(
                active=True,
                reset_at=reset_at,
                message="5h window already active",
            )
        return UsageWindowState(active=False, message="5h window inactive")

    def warm_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Send one tiny Claude messages request and parse 5h reset headers."""
        headers = http.post_capture_headers(
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
        )
        return warmed(
            _parse_header_reset(headers, FIVE_HOUR_HEADER_PREFIX), target
        )


def _parse_header_reset(
    response_headers: dict[str, str],
    prefix: str,
) -> str | None:
    """Parse a unified Claude rate-limit reset header."""
    reset_raw = response_headers.get(f"{prefix}-reset")
    if reset_raw is None:
        return None
    try:
        reset_unix = int(float(reset_raw))
    except TypeError, ValueError:
        return None
    return datetime.fromtimestamp(reset_unix, tz=UTC).isoformat()
