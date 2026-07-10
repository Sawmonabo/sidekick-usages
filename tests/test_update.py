"""Tests for self-update support.

Two surfaces are covered:

* The pure-logic primitives in :mod:`sidekick_usages.update` (version
  parsing, install-method detection, upgrade-command mapping). These
  are pinned with direct unit tests.
* The Typer subcommands ``check-update`` and ``update`` in
  :mod:`sidekick_usages.cli` are exercised via :class:`CliRunner`
  with the ``_FakeHttp`` stand-in pattern from
  :mod:`test_header_path`.
"""

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sidekick_usages import __version__, cli
from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.errors import ForbiddenError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.registry import build_provider_registry
from sidekick_usages.serialization import JsonObject
from sidekick_usages.update import (
    PACKAGE_NAME,
    InstallMethod,
    detect_install_method,
    fetch_latest_release,
    is_newer,
    manual_instructions,
    parse_version,
    upgrade_command_for,
)
from tests.test_support import (
    FixedClock,
    make_account_store,
    make_application_paths,
)


class _FakeHttp(HttpClient):
    """Records calls and returns canned JSON for :meth:`get_json`.

    Inherits from :class:`HttpClient` so the type checker accepts it
    as the ``http`` argument anywhere a real client is expected.
    """

    def __init__(
        self,
        response_json: dict[str, Any] | None = None,
        raise_on_get: Exception | None = None,
    ) -> None:
        """:param response_json: Canned body to return from ``get_json``.

        :param raise_on_get: Optional exception to raise instead of
            returning. Used to simulate rate-limit / network errors.
        """
        super().__init__()
        self.response_json: JsonObject = response_json or {}
        self.raise_on_get = raise_on_get
        self.calls: list[tuple[str, str]] = []
        self.close_calls = 0

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        """Stand-in for :meth:`HttpClient.get_json`."""
        del headers
        self.calls.append(("GET", url))
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.response_json

    def close(self) -> None:
        """Record invocation teardown before closing the fake client."""
        self.close_calls += 1
        super().close()


# -- parse_version -----------------------------------------------
def test_parse_version_strips_leading_v() -> None:
    """Both ``v0.3.0`` and ``0.3.0`` parse identically."""
    assert parse_version("v0.3.0") == (0, 3, 0)
    assert parse_version("0.3.0") == (0, 3, 0)


def test_parse_version_handles_two_segments() -> None:
    """``0.3`` parses to a 2-tuple; comparison still works."""
    assert parse_version("0.3") == (0, 3)


def test_parse_version_rejects_non_numeric_segment() -> None:
    """Garbage in a segment raises ValueError (caller renders it)."""
    with pytest.raises(ValueError, match="invalid literal"):
        parse_version("0.3.x")


# -- is_newer ----------------------------------------------------
def test_is_newer_returns_true_for_strictly_greater() -> None:
    """``0.3.0`` is newer than ``0.2.0``."""
    assert is_newer("0.3.0", "0.2.0") is True


def test_is_newer_returns_false_for_equal() -> None:
    """Equal versions are not newer."""
    assert is_newer("0.2.0", "0.2.0") is False


def test_is_newer_returns_false_for_older() -> None:
    """Earlier versions are not newer."""
    assert is_newer("0.1.0", "0.2.0") is False


# -- fetch_latest_release ----------------------------------------
def test_fetch_latest_release_strips_v_prefix() -> None:
    """``tag_name: v0.3.0`` becomes ``0.3.0``."""
    http = _FakeHttp(response_json={"tag_name": "v0.3.0"})
    assert fetch_latest_release(http) == "0.3.0"


def test_fetch_latest_release_targets_releases_endpoint() -> None:
    """The fetcher hits the ``/releases/latest`` URL."""
    http = _FakeHttp(response_json={"tag_name": "v0.1.0"})
    fetch_latest_release(http)
    assert http.calls == [
        (
            "GET",
            "https://api.github.com/repos/"
            "Sawmonabo/sidekick-usages/releases/latest",
        )
    ]


def test_fetch_latest_release_raises_on_missing_tag() -> None:
    """An empty payload is treated as a parser error."""
    http = _FakeHttp(response_json={})
    with pytest.raises(ValueError, match="tag_name"):
        fetch_latest_release(http)


def test_fetch_latest_release_raises_on_non_string_tag() -> None:
    """A non-string ``tag_name`` is treated as a parser error."""
    http = _FakeHttp(response_json={"tag_name": 42})
    with pytest.raises(ValueError, match="tag_name"):
        fetch_latest_release(http)


def test_fetch_latest_release_propagates_forbidden() -> None:
    """A 403 (rate limit) bubbles up so the CLI can render a hint."""
    http = _FakeHttp(raise_on_get=ForbiddenError("rate limited"))
    with pytest.raises(ForbiddenError):
        fetch_latest_release(http)


# -- detect_install_method ---------------------------------------
def test_detect_install_method_uv() -> None:
    """A path under ``~/.local/share/uv/tools/`` is uv."""
    p = "/home/u/.local/share/uv/tools/sidekick-usages/bin/python"
    assert detect_install_method(p) is InstallMethod.UV


def test_detect_install_method_pipx() -> None:
    """A path under ``pipx/venvs/`` is pipx."""
    p = "/home/u/.local/share/pipx/venvs/sidekick-usages/bin/python"
    assert detect_install_method(p) is InstallMethod.PIPX


def test_detect_install_method_homebrew_macos() -> None:
    """A path under ``Cellar/`` is homebrew (macOS layout)."""
    p = "/opt/homebrew/Cellar/sidekick-usages/0.2.0/libexec/bin/python"
    assert detect_install_method(p) is InstallMethod.HOMEBREW


def test_detect_install_method_homebrew_linux() -> None:
    """A path under ``linuxbrew`` is homebrew."""
    p = "/home/linuxbrew/.linuxbrew/Cellar/sidekick-usages/0.2.0/bin/python"
    assert detect_install_method(p) is InstallMethod.HOMEBREW


def test_detect_install_method_unknown() -> None:
    """A vanilla venv / system Python is UNKNOWN."""
    assert (
        detect_install_method("/usr/local/bin/python") is InstallMethod.UNKNOWN
    )


# -- upgrade_command_for -----------------------------------------
def test_upgrade_command_for_uv() -> None:
    """UV → ``uv tool upgrade <pkg>``."""
    assert upgrade_command_for(InstallMethod.UV) == (
        "uv",
        "tool",
        "upgrade",
        PACKAGE_NAME,
    )


def test_upgrade_command_for_pipx() -> None:
    """PIPX → ``pipx upgrade <pkg>``."""
    assert upgrade_command_for(InstallMethod.PIPX) == (
        "pipx",
        "upgrade",
        PACKAGE_NAME,
    )


def test_upgrade_command_for_homebrew() -> None:
    """HOMEBREW → ``brew upgrade <pkg>``."""
    assert upgrade_command_for(InstallMethod.HOMEBREW) == (
        "brew",
        "upgrade",
        PACKAGE_NAME,
    )


def test_upgrade_command_for_unknown_raises() -> None:
    """UNKNOWN has no upgrade command; the helper refuses to guess."""
    with pytest.raises(ValueError, match="install method"):
        upgrade_command_for(InstallMethod.UNKNOWN)


def test_manual_instructions_lists_three_paths() -> None:
    """Fallback text mentions each supported install method."""
    text = manual_instructions()
    assert "uv tool upgrade" in text
    assert "pipx upgrade" in text
    assert "brew upgrade" in text


# -- CLI: check-update -------------------------------------------
def _install_fake_ctx(http: HttpClient, root: Path) -> None:
    """Inject a context with a fake HTTP client.

    :param http: HTTP stand-in to wire into the CLI context.
    :param root: Test-owned root for Sidekick application paths.
    """
    paths = make_application_paths(root)
    clock = FixedClock()
    cli.set_context(
        cli.AppContext(
            store=make_account_store(root),
            http=http,
            providers=build_provider_registry(clock),
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=cli.Console(),
            err_console=cli.Console(stderr=True),
            clock=clock,
        )
    )


def test_check_update_reports_newer_version(tmp_path: Path) -> None:
    """An advancing ``tag_name`` prints the upgrade hint."""
    http = _FakeHttp(response_json={"tag_name": "v99.0.0"})
    _install_fake_ctx(http, tmp_path)
    result = CliRunner().invoke(cli.app, ["check-update"])
    assert result.exit_code == 0
    assert "99.0.0" in result.stdout
    assert "update" in result.stdout.lower()
    assert http.close_calls == 1


def test_check_update_reports_up_to_date(tmp_path: Path) -> None:
    """When ``tag_name`` matches __version__, no upgrade hint."""
    _install_fake_ctx(
        _FakeHttp(response_json={"tag_name": f"v{__version__}"}),
        tmp_path,
    )
    result = CliRunner().invoke(cli.app, ["check-update"])
    assert result.exit_code == 0
    assert "up to date" in result.stdout.lower()
    assert "sidekick usages · update status" in result.stdout


def test_check_update_handles_rate_limit(tmp_path: Path) -> None:
    """A 403 is rendered as a hint and exits 1 (not a crash)."""
    _install_fake_ctx(
        _FakeHttp(raise_on_get=ForbiddenError("API rate limit")),
        tmp_path,
    )
    result = CliRunner().invoke(cli.app, ["check-update"])
    assert result.exit_code == 1
    assert "rate limit" in result.stderr.lower()
    assert "sidekick usages · update status" not in (
        result.stdout + result.stderr
    )


# -- CLI: update -------------------------------------------------
def test_update_dry_run_prints_command_without_running(tmp_path: Path) -> None:
    """``--dry-run`` echoes the argv and never calls subprocess."""
    _install_fake_ctx(_FakeHttp(), tmp_path)
    with (
        patch(
            "sidekick_usages.cli.detect_install_method",
            return_value=InstallMethod.UV,
        ),
        patch("sidekick_usages.cli.subprocess.run") as run,
    ):
        result = CliRunner().invoke(cli.app, ["update", "--dry-run"])
    assert result.exit_code == 0
    assert "uv tool upgrade sidekick-usages" in result.stdout
    assert ROBOT_LINES[2] not in result.stdout
    run.assert_not_called()


def test_update_invokes_subprocess_for_detected_method(
    tmp_path: Path,
) -> None:
    """Without ``--dry-run``, the detected argv is executed."""
    _install_fake_ctx(_FakeHttp(), tmp_path)
    with (
        patch(
            "sidekick_usages.cli.detect_install_method",
            return_value=InstallMethod.UV,
        ),
        patch(
            "sidekick_usages.cli.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as run,
    ):
        result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 0
    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv == ("uv", "tool", "upgrade", PACKAGE_NAME)


def test_update_unknown_method_exits_with_instructions(
    tmp_path: Path,
) -> None:
    """UNKNOWN exits 1 and emits the manual-upgrade hint."""
    _install_fake_ctx(_FakeHttp(), tmp_path)
    with patch(
        "sidekick_usages.cli.detect_install_method",
        return_value=InstallMethod.UNKNOWN,
    ):
        result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 1
    assert "uv tool upgrade" in result.stderr
