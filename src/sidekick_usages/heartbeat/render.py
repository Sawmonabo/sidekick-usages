"""Rendering helpers for usage-window heartbeat."""

import json
from datetime import UTC, datetime

from rich.console import Console

from sidekick_usages.branding import brand_header
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import (
    ExitCode,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.heartbeat.base import HeartbeatProvider
from sidekick_usages.heartbeat.domain import HeartbeatOutcome
from sidekick_usages.heartbeat.service import heartbeat_supported_label


def render_heartbeat_outcomes(
    outcomes: list[HeartbeatOutcome],
    *,
    console: Console,
    err_console: Console,
    quiet: bool,
) -> None:
    """Render heartbeat outcomes for manual or scheduled runs."""
    for outcome in outcomes:
        render_heartbeat_outcome(
            outcome,
            console=console,
            err_console=err_console,
            quiet=quiet,
        )


def render_heartbeat_outcome(
    outcome: HeartbeatOutcome,
    *,
    console: Console,
    err_console: Console,
    quiet: bool,
) -> None:
    """Render one heartbeat outcome."""
    label = _outcome_label(outcome)
    if quiet and outcome.exit_code == ExitCode.SUCCESS:
        return
    if outcome.status is HeartbeatStatus.WARMED:
        console.print(f"[green]{label}: warmed[/green]")
        return
    if outcome.status is HeartbeatStatus.ACTIVE:
        if not quiet:
            console.print(f"[dim]{label}: active ({outcome.message})[/dim]")
        return
    if outcome.status is HeartbeatStatus.DISABLED:
        if not quiet:
            console.print(f"[dim]{label}: disabled[/dim]")
        return
    if outcome.status in {
        HeartbeatStatus.FAILED,
        HeartbeatStatus.UNSUPPORTED,
    }:
        err_console.print(f"[red]{label}: {outcome.message}[/red]")
        return
    if outcome.status is HeartbeatStatus.ENABLED:
        console.print(f"[green]{label}: enabled[/green]")
        return
    if not quiet:
        console.print(f"{label}: {outcome.message}")


def render_heartbeat_status(
    accounts: list[Account],
    providers: dict[ProviderId, HeartbeatProvider],
    console: Console,
    *,
    json_output: bool = False,
) -> None:
    """Render heartbeat status for account rows."""
    if json_output:
        console.print(
            json.dumps(
                {
                    "accounts": [
                        _heartbeat_status_dict(account, providers)
                        for account in accounts
                    ]
                },
                indent=2,
            )
        )
        return

    console.print(
        brand_header(
            console.size.width,
            section="heartbeat status",
        )
    )
    for index, account in enumerate(accounts):
        if index:
            console.print()
        status = _heartbeat_status_dict(account, providers)
        suffix = f" · {account.plan}" if account.plan != "unknown" else ""
        console.print(f"{account.label}  [{account.provider_id}{suffix}]")
        console.print(f"  heartbeat: {status['heartbeat']}")
        console.print(
            "  supported: "
            + ("yes" if status["heartbeat_supported"] else "no")
        )
        console.print(
            "  enabled: " + ("yes" if status["heartbeat_enabled"] else "no")
        )
        if account.heartbeat_5h_reset_at:
            console.print(
                "  cached 5h reset: "
                + _format_time(account.heartbeat_5h_reset_at)
            )
        if account.heartbeat_window_resets:
            for target_id, reset_at in account.heartbeat_window_resets.items():
                console.print(
                    f"  cached {target_id} reset: {_format_time(reset_at)}"
                )
        if account.heartbeat_targets:
            console.print("  targets: " + ", ".join(account.heartbeat_targets))
        if account.last_heartbeat_status:
            console.print(f"  last heartbeat: {account.last_heartbeat_status}")
        if account.last_heartbeat_error:
            console.print(f"  error: {account.last_heartbeat_error}")


def _heartbeat_status_dict(
    account: Account,
    providers: dict[ProviderId, HeartbeatProvider],
) -> dict[str, object]:
    """Build one account's heartbeat status data for rendering."""
    provider = providers.get(account.provider_id)
    supported = bool(provider and provider.supports(account))
    return {
        "label": account.label,
        "provider": account.provider_id,
        "plan": account.plan,
        "heartbeat": heartbeat_supported_label(account, provider),
        "heartbeat_supported": supported,
        "heartbeat_enabled": account.heartbeat_enabled,
        "heartbeat_5h_reset_at": _optional_time(account.heartbeat_5h_reset_at),
        "heartbeat_window_resets": (
            {
                target_id: _format_time(reset_at)
                for target_id, reset_at in (
                    account.heartbeat_window_resets.items()
                )
            }
            if account.heartbeat_window_resets is not None
            else None
        ),
        "heartbeat_targets": (
            list(account.heartbeat_targets)
            if account.heartbeat_targets is not None
            else None
        ),
        "last_heartbeat_at": _optional_time(account.last_heartbeat_at),
        "last_heartbeat_status": (
            account.last_heartbeat_status.value
            if account.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error": account.last_heartbeat_error,
    }


def _outcome_label(outcome: HeartbeatOutcome) -> str:
    """Render a target-aware account label without changing default output."""
    label = outcome.label or "?"
    if outcome.target_id and outcome.target_id != "standard":
        target = outcome.target_label or outcome.target_id
        return f"{label} [{target}]"
    return label


def _format_time(value: datetime) -> str:
    """Encode one heartbeat timestamp for machine or human output."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_time(value: datetime | None) -> str | None:
    """Encode an optional heartbeat timestamp."""
    return _format_time(value) if value is not None else None
