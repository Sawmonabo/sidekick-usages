"""Heartbeat CLI behavior tests."""

import io
import json
from pathlib import Path

from rich.console import Console

from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.heartbeat.models import HeartbeatOutcome
from sidekick_usages.heartbeat.render import (
    HeartbeatOutputChannel,
    build_heartbeat_status_rows,
    heartbeat_status_json,
    render_heartbeat_outcomes,
    render_heartbeat_status,
)
from tests.fakes.heartbeat import (
    ROUNDTRIP_AUDIT_TIME,
    SPARK_RESET,
    STANDARD_RESET,
    FakeHeartbeatProvider,
    heartbeat_account,
    install_heartbeat_context,
)


def test_heartbeat_enable_disable_and_status_cli(tmp_path: Path) -> None:
    """Heartbeat config is managed through the CLI."""
    provider = FakeHeartbeatProvider()
    harness, store, stdout, _ = install_heartbeat_context(
        tmp_path,
        [heartbeat_account()],
        {ProviderId.CLAUDE: provider},
    )

    enabled = harness.invoke(["heartbeat", "enable", "team"])
    status = harness.invoke(["heartbeat", "status"])
    disabled = harness.invoke(["heartbeat", "disable", "team"])

    assert enabled.exit_code == 0
    assert status.exit_code == 0
    assert disabled.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is False
    rendered = stdout.getvalue()
    assert "enabled" in rendered
    assert "heartbeat: on" in rendered
    assert "disabled" in rendered
    assert rendered.count(ROBOT_LINES[2]) == 1
    assert "heartbeat status" in rendered


def test_heartbeat_status_json_remains_machine_readable(
    tmp_path: Path,
) -> None:
    label = "long-" + "account" * 20
    provider = FakeHeartbeatProvider()
    harness, _, stdout, _ = install_heartbeat_context(
        tmp_path,
        [heartbeat_account(label)],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["accounts"][0]["label"] == label
    assert ROBOT_LINES[2] not in stdout.getvalue()


def test_status_builders_share_one_typed_row_for_human_and_json() -> None:
    """Human and machine views cannot derive different account facts."""
    account = heartbeat_account(
        heartbeat_enabled=True,
        heartbeat_window_resets={
            "standard": STANDARD_RESET,
            "spark": SPARK_RESET,
        },
        heartbeat_targets=("standard", "spark"),
    )
    account.last_heartbeat_at = ROUNDTRIP_AUDIT_TIME
    account.last_heartbeat_status = HeartbeatStatus.WARMED
    rows = build_heartbeat_status_rows(
        (account,),
        {account.label: "on"},
    )

    payload = heartbeat_status_json(rows)
    assert payload == {
        "accounts": [
            {
                "label": "team",
                "provider": "claude",
                "plan": "team",
                "heartbeat": "on",
                "heartbeat_supported": True,
                "heartbeat_enabled": True,
                "heartbeat_window_resets": {
                    "standard": "2026-06-12T18:00:00Z",
                    "spark": "2026-06-12T19:00:00Z",
                },
                "heartbeat_targets": ["standard", "spark"],
                "last_heartbeat_at": "2026-06-12T13:00:00Z",
                "last_heartbeat_status": "warmed",
                "last_heartbeat_error": None,
            }
        ]
    }
    output = io.StringIO()
    Console(file=output, force_terminal=False).print(
        render_heartbeat_status(rows, width=80)
    )
    rendered = output.getvalue()
    assert "heartbeat: on" in rendered
    assert "cached spark reset: 2026-06-12T19:00:00Z" in rendered
    assert "last heartbeat: warmed" in rendered


def test_quiet_outcome_builder_keeps_only_actionable_error_channel() -> None:
    """Scheduled quiet rendering suppresses success but never a failure."""
    outcomes = (
        HeartbeatOutcome(
            label=AccountLabel("healthy"),
            provider_id=ProviderId.CLAUDE,
            status=HeartbeatStatus.WARMED,
            message="warmed",
        ),
        HeartbeatOutcome(
            label=AccountLabel("failed"),
            provider_id=ProviderId.CLAUDE,
            status=HeartbeatStatus.FAILED,
            message="rate limited",
            action_required=True,
            exit_code=ExitCode.MANUAL_ACTION,
        ),
    )

    rendered = render_heartbeat_outcomes(outcomes, quiet=True)

    assert tuple(item.channel for item in rendered) == (
        HeartbeatOutputChannel.STDERR,
    )
    error_output = io.StringIO()
    Console(file=error_output, force_terminal=False).print(
        rendered[0].renderable
    )
    assert error_output.getvalue() == "failed: rate limited\n"


def test_empty_heartbeat_registry_remains_unsupported(
    tmp_path: Path,
) -> None:
    """An explicitly empty registry must not activate default providers."""
    harness, _, stdout, _ = install_heartbeat_context(
        tmp_path,
        [heartbeat_account()],
        {},
    )

    result = harness.invoke(["heartbeat", "status"])

    assert result.exit_code == 0
    assert "heartbeat: unsupported" in stdout.getvalue()
    assert "supported: no" in stdout.getvalue()


def test_heartbeat_label_cli_runs_one_shot_when_disabled(
    tmp_path: Path,
) -> None:
    """The documented heartbeat <label> form runs a one-shot probe."""
    provider = FakeHeartbeatProvider()
    harness, store, stdout, _ = install_heartbeat_context(
        tmp_path,
        [heartbeat_account("team", heartbeat_enabled=False)],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "team"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("team", "old-token")]
    assert "team: warmed" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is False


def test_heartbeat_all_quiet_runs_enabled_only(tmp_path: Path) -> None:
    """Quiet all-account mode is scheduler friendly."""
    provider = FakeHeartbeatProvider()
    harness, _, stdout, _ = install_heartbeat_context(
        tmp_path,
        [
            heartbeat_account("enabled", heartbeat_enabled=True),
            heartbeat_account("disabled", heartbeat_enabled=False),
        ],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "--all", "--quiet"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("enabled", "old-token-enabled")]
    assert stdout.getvalue() == ""
