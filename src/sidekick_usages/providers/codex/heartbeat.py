"""Codex-owned usage-window heartbeat adapter."""

from datetime import datetime

from sidekick_usages.core.models import Account, UsageReport, UsageWindow
from sidekick_usages.core.types import HeartbeatStatus, ProviderId
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
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.providers.codex.request import account_headers
from sidekick_usages.serialization.json import JsonObject

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

    id = ProviderId.CODEX
    display_name = "Codex CLI"

    def __init__(self, usage_provider: CodexProvider) -> None:
        """Build an adapter around the normal Codex usage provider."""
        self._usage_provider = usage_provider

    def supports(self, account: Account) -> bool:
        """Codex can warm saved ChatGPT OAuth accounts."""
        return account.provider_id == self.id and bool(account.access_token)

    def supported_targets(
        self,
        account: Account,
    ) -> tuple[HeartbeatTarget, ...]:
        """Return the standard and separate Spark windows."""
        if not self.supports(account):
            return ()
        return (STANDARD_TARGET, SPARK_TARGET)

    def unsupported_message(self, account: Account) -> str:
        """Return the missing-access detail for Codex accounts."""
        if not account.access_token:
            return "Codex heartbeat requires a saved access token."
        return super().unsupported_message(account)

    def inspect_window(
        self,
        account: ProviderAuthenticatedAccount,
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
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Warm one Codex window and then refresh its usage state."""
        headers = account_headers(runtime_account(account))
        headers["Accept"] = "text/event-stream"
        http.post_capture_headers(
            CODEX_RESPONSES_URL,
            _heartbeat_body(_target_model(target.id)),
            headers,
            operation=HttpOperation.CODEX_HEARTBEAT,
        )
        reset_at = _window_reset(
            self._usage_provider.fetch_usage(account, http),
            target.id,
        )
        if reset_at:
            return warmed(reset_at, target)
        return HeartbeatProbeResult(
            status=HeartbeatStatus.FAILED,
            message=f"{target.label} did not become active after warm",
            warmed=False,
            target_id=target.id,
            target_label=target.label,
        )


def _target_window(
    report: UsageReport,
    target_id: str,
) -> UsageWindow | None:
    if target_id == SPARK_TARGET.id:
        return _spark_window(report)
    return _primary_window(report)


def _primary_window(report: UsageReport) -> UsageWindow | None:
    return next(
        (
            window
            for window in report.windows
            if window.name == FIVE_HOUR_WINDOW
        ),
        None,
    )


def _spark_window(report: UsageReport) -> UsageWindow | None:
    return next(
        (
            window
            for window in report.windows
            if "spark" in window.name.lower()
            and window.name.lower().endswith(" 5h")
        ),
        None,
    )


def _window_reset(report: UsageReport, target_id: str) -> datetime | None:
    window = _target_window(report, target_id)
    return window.resets_at if window else None


def _target_model(target_id: str) -> str:
    if target_id == SPARK_TARGET.id:
        return SPARK_HEARTBEAT_MODEL
    return CODEX_STANDARD_HEARTBEAT_MODEL


def _heartbeat_body(model: str) -> JsonObject:
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
