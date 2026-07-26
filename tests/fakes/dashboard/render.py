"""Reusable secret-free interactive dashboard render state."""

from datetime import date, datetime, timedelta

from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardActivity,
    DashboardCursor,
    DashboardExternalRow,
    DashboardFooter,
    DashboardFooterKind,
    DashboardProvider,
    DashboardService,
    DashboardSnapshot,
    DashboardUsage,
)

CLAUDE_ACTIVE_ID = SidekickAccountId(
    "11111111-1111-4111-8111-111111111111"
)
CLAUDE_WARNING_ID = SidekickAccountId(
    "22222222-2222-4222-8222-222222222222"
)
CODEX_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
FORBIDDEN_SELECTION_LABELS = (
    "IN USE",
    "ACTIVATING",
    "MIGRATION REQUIRED",
    "CURRENT",
)
PROGRESS_COPY = (
    "Switching to personal@example.test… verifying with Claude Code"
)


def interactive_dashboard_state(
    reference_time: datetime,
) -> tuple[DashboardSnapshot, DashboardCursor, DashboardFooter]:
    """Build one wide/narrow render state for controller and PTY journeys."""
    reset_at = reference_time + timedelta(hours=3, minutes=50)
    observed_at = reference_time - timedelta(hours=2, minutes=14)
    claude = DashboardProvider(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        active_account_id=CLAUDE_ACTIVE_ID,
        verified_at=observed_at,
        actions_enabled=True,
        rows=(
            DashboardAccount(
                account_id=CLAUDE_ACTIVE_ID,
                label=AccountLabel("work@example.test"),
                provider_id=ProviderId.CLAUDE,
                plan="max",
                credential_health=CredentialHealth.HEALTHY,
                active=True,
                states=(
                    DashboardActionState.HEALTHY,
                    DashboardActionState.METRICS_STALE,
                ),
                usage=DashboardUsage(
                    plan="max",
                    report=_report(reset_at, 0, 51, plan="max"),
                    observed_at=observed_at,
                ),
                activity=DashboardActivity(
                    summary=TokenActivitySummary(
                        total_tokens=903_464_085,
                        scope=TokenActivityScope.LOCAL_INSTALLATION,
                        since=date(2025, 12, 28),
                    ),
                    observed_at=observed_at,
                ),
            ),
            DashboardAccount(
                account_id=CLAUDE_WARNING_ID,
                label=AccountLabel("personal@example.test"),
                provider_id=ProviderId.CLAUDE,
                plan="max",
                credential_health=CredentialHealth.LOGIN_REQUIRED,
                active=False,
                states=(DashboardActionState.LOGIN_REQUIRED,),
            ),
        ),
    )
    codex = DashboardProvider(
        provider_id=ProviderId.CODEX,
        runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
        active_account_id=None,
        verified_at=observed_at,
        actions_enabled=True,
        rows=(
            DashboardAccount(
                account_id=CODEX_ID,
                label=AccountLabel("codex@example.test"),
                provider_id=ProviderId.CODEX,
                plan="pro",
                credential_health=CredentialHealth.HEALTHY,
                active=False,
                states=(DashboardActionState.HEALTHY,),
                usage=DashboardUsage(
                    plan="pro",
                    report=_report(reset_at, 8, 45, plan="pro"),
                    observed_at=observed_at,
                ),
                activity=DashboardActivity(
                    summary=TokenActivitySummary(
                        total_tokens=7_449_473_297,
                        scope=TokenActivityScope.ACCOUNT,
                        since=date(2026, 4, 7),
                    ),
                    observed_at=observed_at,
                ),
            ),
            DashboardExternalRow(
                provider_id=ProviderId.CODEX,
                observed_at=observed_at,
                states=(DashboardActionState.EXTERNAL_ACTIVE,),
            ),
        ),
    )
    return (
        DashboardSnapshot(
            providers=(claude, codex),
            service=DashboardService(
                ready=True,
                compatible=True,
                phase=ServicePhase.READY,
                observed_at=observed_at,
                failure_code=None,
            ),
            reference_time=reference_time,
        ),
        DashboardCursor(
            focused_provider=ProviderId.CLAUDE,
            account_id=CLAUDE_ACTIVE_ID,
        ),
        DashboardFooter(
            kind=DashboardFooterKind.PROGRESS,
            message=PROGRESS_COPY,
        ),
    )


def _report(
    reset_at: datetime,
    short_utilization: float,
    long_utilization: float,
    *,
    plan: str,
) -> UsageReport:
    return UsageReport(
        windows=(
            UsageWindow("5h", short_utilization, reset_at),
            UsageWindow("7d", long_utilization, reset_at),
        ),
        plan=plan,
    )
