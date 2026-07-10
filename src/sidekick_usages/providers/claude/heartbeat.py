"""Claude usage-window heartbeat adapter."""

from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ProviderId
from sidekick_usages.heartbeat.models import (
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider, warmed
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.claude.schemas import (
    header_reset,
    provider_time,
)
from sidekick_usages.providers.claude.usage import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_BETA,
    MESSAGES_URL,
    PROBE_MODEL,
    PROFILE_SCOPE,
    USAGE_URL,
    USER_AGENT,
)

INFERENCE_SCOPE = "user:inference"
FIVE_HOUR_KEY = "five_hour"
FIVE_HOUR_HEADER_PREFIX = "anthropic-ratelimit-unified-5h"


class ClaudeHeartbeat(HeartbeatProvider):
    """Window warming for Claude OAuth and setup-token accounts."""

    id = ProviderId.CLAUDE
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
        """Read Claude usage through the OAuth route when available."""
        del target
        if account.scopes is not None and PROFILE_SCOPE not in account.scopes:
            return UsageWindowState(
                active=False,
                message="header probe needed",
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
            return UsageWindowState(
                active=False,
                message="5h window missing",
            )
        reset_at = provider_time(window.get("resets_at"))
        if reset_at is not None:
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
        """Send one tiny Claude request and parse its reset header."""
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
            operation=HttpOperation.CLAUDE_HEARTBEAT,
        )
        return warmed(
            header_reset(headers, FIVE_HOUR_HEADER_PREFIX),
            target,
        )
