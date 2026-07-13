"""Behavioral tests for Sidekick-owned path composition."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import sidekick_usages.paths as paths_module
from sidekick_usages.paths import PathDiscoveryError
from sidekick_usages.persistence.account_store import AccountStoreStateError
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from tests.test_support import make_account_store, make_application_paths


@dataclass(frozen=True, slots=True)
class _NativePaths:
    user_data_path: Path


@pytest.mark.parametrize(
    ("platform_name", "data_root", "environment"),
    [
        (
            "linux",
            "/home/alice/.local/share/sidekick-usages",
            {},
        ),
        (
            "linux",
            "/srv/alice/data/sidekick-usages",
            {
                "XDG_DATA_HOME": "/srv/alice/data",
            },
        ),
        (
            "linux",
            "/home/alice/.local/share/sidekick-usages",
            {
                "LOCALAPPDATA": "C:/Users/Alice/AppData/Local",
                "WIN_PD_OVERRIDE_LOCAL_APPDATA": "C:/test-only",
                "WSL_INTEROP": "/run/WSL/1_interop",
            },
        ),
        (
            "darwin",
            "/Users/alice/Library/Application Support/sidekick-usages",
            {},
        ),
        (
            "win32",
            "C:/Users/Alice/AppData/Local/sidekick-usages",
            {"WIN_PD_OVERRIDE_LOCAL_APPDATA": ""},
        ),
    ],
    ids=("linux", "linux-xdg", "wsl", "macos", "windows"),
)
def test_discovery_maps_the_frozen_native_matrix_without_side_effects(
    platform_name: str,
    data_root: str,
    environment: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native roots map to files while compatibility inputs stay distinct."""
    for name in (
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name.startswith("WIN_PD_OVERRIDE_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    home = tmp_path / "absent-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(paths_module.sys, "platform", platform_name)
    calls: list[dict[str, object]] = []

    def platform_dirs(**arguments: object) -> _NativePaths:
        calls.append(arguments)
        return _NativePaths(Path(data_root))

    monkeypatch.setattr(paths_module, "PlatformDirs", platform_dirs)

    paths = paths_module.discover_application_paths()

    compatibility_root = home / ".config" / "sidekick-usages"
    assert paths.accounts.canonical == Path(data_root) / "accounts.json"
    assert (
        paths.accounts.existing_sidekick
        == compatibility_root / "accounts.json"
    )
    assert paths.accounts.prototype_cc_usage == (
        home / ".config" / "cc-usage" / "accounts.json"
    )
    assert paths.private_codex.canonical == Path(data_root) / "codex"
    assert (
        paths.private_codex.existing_sidekick == compatibility_root / "codex"
    )
    assert paths.activity_snapshots == Path(data_root) / "token-activity.json"
    assert paths.credential_refresh == Path(data_root) / "credential-refresh"
    assert calls == [
        {
            "appname": "sidekick-usages",
            "appauthor": False,
            "version": None,
            "roaming": False,
            "multipath": False,
            "opinion": True,
            "ensure_exists": False,
            "use_site_for_root": False,
        }
    ]
    assert not home.exists()


@pytest.mark.parametrize(
    ("platform_name", "variable", "value"),
    [
        ("linux", "XDG_DATA_HOME", "relative/data"),
        (
            "win32",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "C:/test-only/local",
        ),
        ("win32", "WIN_PD_OVERRIDE_APPDATA", "C:/test-only/roaming"),
        ("win32", "WIN_PD_OVERRIDE_CACHE", "   "),
        ("win32", "WIN_PD_OVERRIDE_FUTURE", "C:/test-only/future"),
    ],
)
def test_discovery_rejects_unsafe_environment_before_resolving_paths(
    platform_name: str,
    variable: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative XDG homes and Windows library overrides fail closed."""
    for name in (
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name.startswith("WIN_PD_OVERRIDE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(paths_module.sys, "platform", platform_name)

    def unexpected_platform_dirs(**_arguments: object) -> _NativePaths:
        pytest.fail("PlatformDirs must not run for an unsafe environment.")

    monkeypatch.setattr(
        paths_module,
        "PlatformDirs",
        unexpected_platform_dirs,
    )

    with pytest.raises(PathDiscoveryError) as exc_info:
        paths_module.discover_application_paths()

    assert exc_info.value.variable == variable
    assert variable in str(exc_info.value)
    assert value not in str(exc_info.value)


def test_account_store_does_not_implicitly_import_prototype(
    tmp_path: Path,
) -> None:
    """Runtime loading leaves prototype migration to the coordinator."""
    paths = make_application_paths(tmp_path)
    prototype_file = paths.accounts.prototype_cc_usage
    prototype_content = json.dumps(
        {"team": {"token": "secret", "plan": "max"}}
    )
    prototype_filesystem = PersistenceFilesystem(prototype_file)
    prototype_filesystem._prepare_parent()
    prototype_filesystem._native.create_private(
        prototype_file.parent,
        prototype_file.name,
        prototype_content.encode(),
    )

    with pytest.raises(AccountStoreStateError) as exc_info:
        make_account_store(tmp_path)

    assert exc_info.value.code is PersistenceCode.PROTOTYPE_IMPORT_REQUIRED
    assert not paths.accounts.canonical.exists()
    assert prototype_file.read_text() == prototype_content
