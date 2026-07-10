"""Pure rendering contracts for usage-window heartbeat."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from rich.console import Group, RenderableType
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.heartbeat.models import HeartbeatOutcome
from sidekick_usages.serialization import JsonObject, JsonValue


class HeartbeatOutputChannel(StrEnum):
    """Terminal channel selected for one heartbeat renderable."""

    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class HeartbeatChannelRenderable:
    """One ordered heartbeat renderable and its terminal channel."""

    channel: HeartbeatOutputChannel
    renderable: RenderableType


@dataclass(frozen=True, slots=True)
class HeartbeatStatusRow:
    """Immutable display data shared by heartbeat human and JSON builders."""

    label: AccountLabel
    provider_id: ProviderId
    plan: str
    heartbeat: str
    heartbeat_supported: bool
    heartbeat_enabled: bool
    heartbeat_5h_reset_at: str | None
    heartbeat_window_resets: tuple[tuple[str, str], ...] | None
    heartbeat_targets: tuple[str, ...] | None
    last_heartbeat_at: str | None
    last_heartbeat_status: HeartbeatStatus | None
    last_heartbeat_error: str | None


def render_heartbeat_outcomes(
    outcomes: Sequence[HeartbeatOutcome],
    *,
    quiet: bool,
) -> tuple[HeartbeatChannelRenderable, ...]:
    """Build ordered channel renderables for heartbeat outcomes."""
    return tuple(
        rendered
        for outcome in outcomes
        if (rendered := render_heartbeat_outcome(outcome, quiet=quiet))
        is not None
    )


def render_heartbeat_outcome(
    outcome: HeartbeatOutcome,
    *,
    quiet: bool,
) -> HeartbeatChannelRenderable | None:
    """Build one channel renderable, or suppress successful quiet output."""
    label = _outcome_label(outcome)
    if quiet and outcome.exit_code == ExitCode.SUCCESS:
        return None
    if outcome.status is HeartbeatStatus.WARMED:
        rendered = _stdout(Text(f"{label}: warmed", style="green"))
    elif outcome.status is HeartbeatStatus.ACTIVE:
        rendered = _stdout(
            Text(f"{label}: active ({outcome.message})", style="dim")
        )
    elif outcome.status is HeartbeatStatus.DISABLED:
        rendered = _stdout(Text(f"{label}: disabled", style="dim"))
    elif outcome.status in {
        HeartbeatStatus.FAILED,
        HeartbeatStatus.UNSUPPORTED,
    }:
        rendered = HeartbeatChannelRenderable(
            HeartbeatOutputChannel.STDERR,
            Text(f"{label}: {outcome.message}", style="red"),
        )
    elif outcome.status is HeartbeatStatus.ENABLED:
        rendered = _stdout(Text(f"{label}: enabled", style="green"))
    else:
        rendered = _stdout(Text(f"{label}: {outcome.message}"))
    return rendered


def build_heartbeat_status_rows(
    accounts: Sequence[Account],
    support_labels: Mapping[AccountLabel, str],
) -> tuple[HeartbeatStatusRow, ...]:
    """Build immutable status rows from completed account and support data."""
    return tuple(
        _heartbeat_status_row(account, support_labels) for account in accounts
    )


def render_heartbeat_status(
    rows: Sequence[HeartbeatStatusRow],
    *,
    width: int,
) -> RenderableType:
    """Build the human heartbeat status view without printing."""
    parts: list[RenderableType] = [
        brand_header(width, section="heartbeat status")
    ]
    for index, row in enumerate(rows):
        if index:
            parts.append(Text(""))
        suffix = f" · {row.plan}" if row.plan != "unknown" else ""
        parts.extend(
            (
                Text.from_markup(f"{row.label}  [{row.provider_id}{suffix}]"),
                Text(f"  heartbeat: {row.heartbeat}"),
                Text(
                    "  supported: "
                    + ("yes" if row.heartbeat_supported else "no")
                ),
                Text(
                    "  enabled: " + ("yes" if row.heartbeat_enabled else "no")
                ),
            )
        )
        if row.heartbeat_5h_reset_at is not None:
            parts.append(
                Text(f"  cached 5h reset: {row.heartbeat_5h_reset_at}")
            )
        if row.heartbeat_window_resets:
            parts.extend(
                Text(f"  cached {target_id} reset: {reset_at}")
                for target_id, reset_at in row.heartbeat_window_resets
            )
        if row.heartbeat_targets:
            parts.append(
                Text("  targets: " + ", ".join(row.heartbeat_targets))
            )
        if row.last_heartbeat_status is not None:
            parts.append(
                Text(f"  last heartbeat: {row.last_heartbeat_status}")
            )
        if row.last_heartbeat_error:
            parts.append(Text(f"  error: {row.last_heartbeat_error}"))
    return Group(*parts)


def heartbeat_status_json(
    rows: Sequence[HeartbeatStatusRow],
) -> JsonObject:
    """Build recursively typed heartbeat status JSON data."""
    accounts: list[JsonValue] = [_heartbeat_status_dict(row) for row in rows]
    return {"accounts": accounts}


def _heartbeat_status_row(
    account: Account,
    support_labels: Mapping[AccountLabel, str],
) -> HeartbeatStatusRow:
    support_label = support_labels[account.label]
    return HeartbeatStatusRow(
        label=account.label,
        provider_id=account.provider_id,
        plan=account.plan,
        heartbeat=support_label,
        heartbeat_supported=support_label != "unsupported",
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_5h_reset_at=_optional_time(account.heartbeat_5h_reset_at),
        heartbeat_window_resets=(
            tuple(
                (target_id, _format_time(reset_at))
                for target_id, reset_at in (
                    account.heartbeat_window_resets.items()
                )
            )
            if account.heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=_optional_time(account.last_heartbeat_at),
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error=account.last_heartbeat_error,
    )


def _heartbeat_status_dict(row: HeartbeatStatusRow) -> JsonObject:
    window_resets: JsonValue = None
    if row.heartbeat_window_resets is not None:
        encoded_resets: JsonObject = {}
        for target_id, reset_at in row.heartbeat_window_resets:
            encoded_resets[target_id] = reset_at
        window_resets = encoded_resets
    targets: JsonValue = None
    if row.heartbeat_targets is not None:
        encoded_targets: list[JsonValue] = []
        encoded_targets.extend(row.heartbeat_targets)
        targets = encoded_targets
    return {
        "label": str(row.label),
        "provider": row.provider_id.value,
        "plan": row.plan,
        "heartbeat": row.heartbeat,
        "heartbeat_supported": row.heartbeat_supported,
        "heartbeat_enabled": row.heartbeat_enabled,
        "heartbeat_5h_reset_at": row.heartbeat_5h_reset_at,
        "heartbeat_window_resets": window_resets,
        "heartbeat_targets": targets,
        "last_heartbeat_at": row.last_heartbeat_at,
        "last_heartbeat_status": (
            row.last_heartbeat_status.value
            if row.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error": row.last_heartbeat_error,
    }


def _stdout(renderable: RenderableType) -> HeartbeatChannelRenderable:
    return HeartbeatChannelRenderable(
        HeartbeatOutputChannel.STDOUT,
        renderable,
    )


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


__all__ = [
    "HeartbeatChannelRenderable",
    "HeartbeatOutputChannel",
    "HeartbeatStatusRow",
    "build_heartbeat_status_rows",
    "heartbeat_status_json",
    "render_heartbeat_outcome",
    "render_heartbeat_outcomes",
    "render_heartbeat_status",
]
