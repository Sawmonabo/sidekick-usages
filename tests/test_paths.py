"""Behavioral tests for current Sidekick-owned path composition."""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import sidekick_usages.paths
from sidekick_usages.paths import PathDiscoveryError


@dataclass(frozen=True, slots=True)
class _NativePaths:
    user_data_path: Path
    user_runtime_path: Path
    user_log_path: Path


@pytest.mark.parametrize(
    ("platform_name", "data_root", "environment"),
    [
        ("linux", "/home/alice/.local/share/sidekick-usages", {}),
        (
            "linux",
            "/srv/alice/data/sidekick-usages",
            {"XDG_DATA_HOME": "/srv/alice/data"},
        ),
        (
            "linux",
            "/home/alice/.local/share/sidekick-usages",
            {"WSL_INTEROP": "/run/WSL/1_interop"},
        ),
        (
            "darwin",
            "/Users/alice/Library/Application Support/sidekick-usages",
            {},
        ),
        (
            "win32",
            "C:/Users/Alice/AppData/Local/sidekick-usages",
            {},
        ),
    ],
    ids=("linux", "linux-xdg", "wsl", "macos", "windows"),
)
def test_discovery_maps_platform_roots_to_one_current_layout(
    platform_name: str,
    data_root: str,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("XDG_DATA_HOME",):
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name.startswith("WIN_PD_OVERRIDE_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sidekick_usages.paths.sys, "platform", platform_name)

    root = Path(data_root)
    monkeypatch.setattr(
        sidekick_usages.paths,
        "PlatformDirs",
        lambda **_arguments: _NativePaths(
            root,
            root / "runtime",
            root / "logs",
        ),
    )

    paths = sidekick_usages.paths.discover_application_paths()

    assert paths.accounts == root / "accounts.json"
    assert paths.private_credentials == root / "credentials"
    assert paths.private_codex_profiles == root / "credentials" / "codex"
    assert paths.private_claude_profiles == root / "credentials" / "claude"
    assert paths.activity_snapshots == root / "token-activity.json"
    assert paths.usage_snapshots == root / "usage-metrics.json"
    assert paths.credential_refresh == root / "credential-refresh"
    assert paths.selected_state == root / "selected-accounts.json"
    assert paths.activation_journals == root / "activation-journals"
    assert paths.durable_operations == root / "operations"
    assert paths.service_state == root / "service-state.json"
    assert paths.service_setup_acknowledgement == (
        root / "service-setup-acknowledgement.json"
    )
    assert paths.service_logs == root / "logs"
    assert paths.supervisor_socket == root / "runtime" / "supervisor.sock"
    assert paths.systemd_user_service == (
        Path.home()
        / ".config"
        / "systemd"
        / "user"
        / "sidekick-usages.service"
    )
    assert paths.launch_agent == (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / "com.sidekick-usages.supervisor.plist"
    )


@pytest.mark.parametrize(
    ("platform_name", "variable", "value"),
    [
        ("linux", "XDG_DATA_HOME", "relative/data"),
        (
            "win32",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "C:/test-only/local",
        ),
    ],
)
def test_discovery_rejects_unsafe_environment_before_platform_resolution(
    platform_name: str,
    variable: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for name in tuple(os.environ):
        if name.startswith("WIN_PD_OVERRIDE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(sidekick_usages.paths.sys, "platform", platform_name)
    monkeypatch.setattr(
        sidekick_usages.paths,
        "PlatformDirs",
        lambda **_arguments: pytest.fail(
            "Unsafe environments must fail before PlatformDirs."
        ),
    )

    with pytest.raises(PathDiscoveryError) as failure:
        sidekick_usages.paths.discover_application_paths()

    assert failure.value.variable == variable
    assert value not in str(failure.value)
