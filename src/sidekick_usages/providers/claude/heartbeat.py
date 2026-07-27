"""Claude usage-window heartbeat adapter."""

from typing import assert_never

from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.heartbeat.models import (
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider, warmed
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.providers.base import (
    ProviderAuthenticatedAccount,
    runtime_account,
)
from sidekick_usages.providers.claude.credentials import (
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schema.usage import (
    header_reset,
    oauth_usage_window,
)
from sidekick_usages.providers.claude.usage import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_BETA,
    MESSAGES_URL,
    PROBE_MODEL,
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
        """Claude can warm setup tokens or inference-capable logins."""
        if account.provider_id != self.id:
            return False
        credentials = require_claude_credentials(account)
        match credentials:
            case ClaudeSetupTokenCredentials():
                return True
            case ClaudeLoginCredentials(scopes=scopes):
                return INFERENCE_SCOPE in scopes
            case unexpected:
                assert_never(unexpected)

    def unsupported_message(self, account: Account) -> str:
        """Return the missing-scope detail for Claude accounts."""
        credentials = require_claude_credentials(account)
        if isinstance(credentials, ClaudeLoginCredentials):
            return (
                "Claude heartbeat requires user:inference scope to send "
                "the tiny warming request."
            )
        return super().unsupported_message(account)

    def inspect_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        """Inspect logins without letting setup tokens impersonate them."""
        del target
        runtime = runtime_account(account)
        credentials = require_claude_credentials(runtime)
        match credentials:
            case ClaudeSetupTokenCredentials():
                return UsageWindowState(
                    active=False,
                    message="header probe needed",
                )
            case ClaudeLoginCredentials():
                pass
            case unexpected:
                assert_never(unexpected)
        data = http.get_json(
            USAGE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {runtime.access_token}",
                "User-Agent": USER_AGENT,
                "anthropic-beta": ANTHROPIC_BETA,
            },
        )
        window = oauth_usage_window(data.get(FIVE_HOUR_KEY), "5h")
        if window is None:
            return UsageWindowState(
                active=False,
                message="5h window missing",
            )
        if window.resets_at is not None:
            return UsageWindowState(
                active=True,
                reset_at=window.resets_at,
                message="5h window already active",
            )
        return UsageWindowState(active=False, message="5h window inactive")

    def warm_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Send one tiny Claude request and parse its reset header."""
        runtime = runtime_account(account)
        response = http.post_capture_headers(
            MESSAGES_URL,
            {
                "model": PROBE_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "quota"}],
            },
            {
                "Authorization": f"Bearer {runtime.access_token}",
                "anthropic-version": ANTHROPIC_API_VERSION,
                "anthropic-beta": ANTHROPIC_BETA,
                "User-Agent": USER_AGENT,
            },
            operation=HttpOperation.CLAUDE_HEARTBEAT,
        )
        return warmed(
            header_reset(response.headers, FIVE_HOUR_HEADER_PREFIX),
            target,
        )
