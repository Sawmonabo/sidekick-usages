"""Codex-specific usage-window heartbeat adapter."""

from sidekick_usages.errors import UsageError
from sidekick_usages.heartbeat.base import HeartbeatProvider, warmed
from sidekick_usages.heartbeat.domain import (
    HEARTBEAT_FAILED,
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.codex import USER_AGENT, CodexProvider
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_STANDARD_HEARTBEAT_MODEL = "gpt-5.4-mini"
SPARK_HEARTBEAT_MODEL = "gpt-5.3-codex-spark"
FIVE_HOUR_WINDOW = "5h"
STANDARD_TARGET = HeartbeatTarget(
    id="standard",
    label="Codex 5h",
    default=True,
)
SPARK_TARGET = HeartbeatTarget(
    id="spark",
    label="Codex Spark 5h",
)


class CodexHeartbeat(HeartbeatProvider):
    """Window warming for saved Codex/ChatGPT accounts."""

    id = "codex"
    display_name = "Codex CLI"

    def __init__(self, usage_provider: CodexProvider | None = None) -> None:
        """Build an adapter around the normal Codex usage provider."""
        self._usage_provider = usage_provider or CodexProvider()

    def supports(self, account: Account) -> bool:
        """Codex can warm saved ChatGPT OAuth accounts."""
        return account.provider_id == self.id and bool(account.access_token)

    def supported_targets(
        self, account: Account
    ) -> tuple[HeartbeatTarget, ...]:
        """Codex exposes a standard window plus a separate Spark window."""
        if not self.supports(account):
            return ()
        return (STANDARD_TARGET, SPARK_TARGET)

    def unsupported_message(self, account: Account) -> str:
        if not account.access_token:
            return "Codex heartbeat requires a saved access token."
        return super().unsupported_message(account)

    def inspect_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        """Read Codex usage state without sending a model request."""
        window = _target_window(
            self._usage_provider.fetch_usage(account, http),
            target.id,
        )
        if window is None:
            return UsageWindowState(
                active=False,
                message=f"{target.label} window missing",
            )
        if window.resets_at:
            return UsageWindowState(
                active=True,
                reset_at=window.resets_at,
                message=f"{target.label} window already active",
            )
        return UsageWindowState(
            active=False,
            message=f"{target.label} window inactive",
        )

    def warm_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Send one tiny Codex Responses request, then refresh usage state."""
        account_id = _account_id(account)
        http.post_capture_headers(
            CODEX_RESPONSES_URL,
            _heartbeat_body(_target_model(target.id)),
            {
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {account.access_token}",
                "ChatGPT-Account-ID": account_id,
                "User-Agent": USER_AGENT,
            },
        )
        reset_at = _window_reset(
            self._usage_provider.fetch_usage(account, http),
            target.id,
        )
        if reset_at:
            return warmed(reset_at, target)
        return HeartbeatProbeResult(
            status=HEARTBEAT_FAILED,
            message=f"{target.label} did not become active after warm",
            warmed=False,
            target_id=target.id,
            target_label=target.label,
        )


def _account_id(account: Account) -> str:
    """Return the saved ChatGPT account id required by Codex backend."""
    if account.provider_account_id:
        return account.provider_account_id
    raise UsageError(
        "Missing Codex account id. Run sidekick-usages refresh "
        f"{account.label} before heartbeat."
    )


def _primary_window(report: UsageReport) -> UsageWindow | None:
    """Return Codex's primary 5h window from a usage report."""
    for window in report.windows:
        if window.name == FIVE_HOUR_WINDOW:
            return window
    return None


def _spark_window(report: UsageReport) -> UsageWindow | None:
    """Return Codex Spark's separate 5h window from a usage report."""
    for window in report.windows:
        lower_name = window.name.lower()
        if "spark" in lower_name and lower_name.endswith(" 5h"):
            return window
    return None


def _target_window(
    report: UsageReport,
    target_id: str,
) -> UsageWindow | None:
    """Return the usage window associated with a heartbeat target."""
    if target_id == SPARK_TARGET.id:
        return _spark_window(report)
    return _primary_window(report)


def _window_reset(report: UsageReport, target_id: str) -> str | None:
    """Return the 5h reset timestamp after warming, when available."""
    window = _target_window(report, target_id)
    return window.resets_at if window else None


def _target_model(target_id: str) -> str:
    """Return the model that warms one Codex usage target."""
    if target_id == SPARK_TARGET.id:
        return SPARK_HEARTBEAT_MODEL
    return CODEX_STANDARD_HEARTBEAT_MODEL


def _heartbeat_body(model: str) -> dict[str, object]:
    """Build the smallest Codex Responses request shape we can justify."""
    return {
        "model": model,
        "instructions": "Reply with exactly: ok",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ok"}],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low"},
        "store": False,
        "stream": True,
        "include": [],
    }
