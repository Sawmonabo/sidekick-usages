"""Rendering helpers for usage-window heartbeat."""

import json

from rich.console import Console

from sidekick_usages.heartbeat.base import HeartbeatProvider
from sidekick_usages.heartbeat.domain import (
    HEARTBEAT_ACTIVE,
    HEARTBEAT_DISABLED,
    HEARTBEAT_ENABLED,
    HEARTBEAT_FAILED,
    HEARTBEAT_UNSUPPORTED,
    HEARTBEAT_WARMED,
    HeartbeatOutcome,
)
from sidekick_usages.heartbeat.service import heartbeat_supported_label
from sidekick_usages.store import Account


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
    if quiet and outcome.exit_code == 0:
        return
    if outcome.status == HEARTBEAT_WARMED:
        console.print(f"[green]{label}: warmed[/green]")
        return
    if outcome.status == HEARTBEAT_ACTIVE:
        if not quiet:
            console.print(f"[dim]{label}: active ({outcome.message})[/dim]")
        return
    if outcome.status == HEARTBEAT_DISABLED:
        if not quiet:
            console.print(f"[dim]{label}: disabled[/dim]")
        return
    if outcome.status in {HEARTBEAT_FAILED, HEARTBEAT_UNSUPPORTED}:
        err_console.print(f"[red]{label}: {outcome.message}[/red]")
        return
    if outcome.status == HEARTBEAT_ENABLED:
        console.print(f"[green]{label}: enabled[/green]")
        return
    if not quiet:
        console.print(f"{label}: {outcome.message}")


def render_heartbeat_status(
    accounts: list[Account],
    providers: dict[str, HeartbeatProvider],
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
                        heartbeat_status_dict(account, providers)
                        for account in accounts
                    ]
                },
                indent=2,
            )
        )
        return

    for index, account in enumerate(accounts):
        if index:
            console.print()
        status = heartbeat_status_dict(account, providers)
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
                f"  cached 5h reset: {account.heartbeat_5h_reset_at}"
            )
        if account.heartbeat_window_resets:
            for target_id, reset_at in account.heartbeat_window_resets.items():
                console.print(f"  cached {target_id} reset: {reset_at}")
        if account.heartbeat_targets:
            console.print("  targets: " + ", ".join(account.heartbeat_targets))
        if account.last_heartbeat_status:
            console.print(f"  last heartbeat: {account.last_heartbeat_status}")
        if account.last_heartbeat_error:
            console.print(f"  error: {account.last_heartbeat_error}")


def heartbeat_status_dict(
    account: Account,
    providers: dict[str, HeartbeatProvider],
) -> dict[str, object]:
    """Return one account's heartbeat status for status/doctor/list."""
    provider = providers.get(account.provider_id)
    supported = bool(provider and provider.supports(account))
    return {
        "label": account.label,
        "provider": account.provider_id,
        "plan": account.plan,
        "heartbeat": heartbeat_supported_label(account, provider),
        "heartbeat_supported": supported,
        "heartbeat_enabled": account.heartbeat_enabled,
        "heartbeat_5h_reset_at": account.heartbeat_5h_reset_at,
        "heartbeat_window_resets": account.heartbeat_window_resets,
        "heartbeat_targets": account.heartbeat_targets,
        "last_heartbeat_at": account.last_heartbeat_at,
        "last_heartbeat_status": account.last_heartbeat_status,
        "last_heartbeat_error": account.last_heartbeat_error,
    }


def _outcome_label(outcome: HeartbeatOutcome) -> str:
    """Render a target-aware account label without changing default output."""
    if outcome.target_id and outcome.target_id != "standard":
        target = outcome.target_label or outcome.target_id
        return f"{outcome.label} [{target}]"
    return outcome.label
