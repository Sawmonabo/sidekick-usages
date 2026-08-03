"""Public session enrollment and capability-refusal behavior."""

import os
from pathlib import Path

import click
import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts.session import SessionContext
from sidekick_usages.cli.session.launcher import ProviderSessionLauncher
from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
    ShellIntegrationError,
    ShellIntegrationFailure,
    ShellKind,
)
from sidekick_usages.cli.session.shell import (
    ShellEnrollment,
    ShellStartupResolver,
)
from sidekick_usages.core.types import ExitCode, ProviderId

_ORIGINAL_BASH = "# user alias\nalias ll='ls -l'\n"
_POSIX_FUNCTIONS = """claude() {
    command sidekick-usages session claude -- "$@"
}
codex() {
    command sidekick-usages session codex -- "$@"
}
"""
_FISH_FUNCTIONS = """function claude
    command sidekick-usages session claude -- $argv
end
function codex
    command sidekick-usages session codex -- $argv
end
"""
_PROVIDER_EXIT_CODE = 17
_PRIVATE_FILE_MODE = 0o600


def _executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)


def test_launcher_freezes_process_contract_and_rejects_unsafe_override(
    tmp_path: Path,
) -> None:
    """Mutating argv or allowing protected config would break enrollment."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    claude = binaries / "claude"
    codex = binaries / "codex"
    _executable(claude)
    _executable(codex)
    calls: list[tuple[tuple[str, ...], dict[str, str], Path]] = []

    def run_process(
        command: tuple[str, ...],
        environment: dict[str, str],
        working_directory: Path,
    ) -> int:
        calls.append((command, environment, working_directory))
        return _PROVIDER_EXIT_CODE

    launcher = ProviderSessionLauncher(
        {"PATH": str(binaries), "TERM": "xterm-256color"},
        working_directory=tmp_path,
        process_runner=run_process,
    )
    arguments = ("--model", "sonnet", "prompt with spaces")

    spec = launcher.plan(ProviderId.CLAUDE, arguments)

    assert spec.provider_arguments == arguments
    assert spec.command == (str(claude), *arguments)
    assert spec.working_directory == tmp_path
    assert launcher.run(spec) == _PROVIDER_EXIT_CODE
    assert calls == [
        (
            (str(claude), *arguments),
            {"PATH": str(binaries), "TERM": "xterm-256color"},
            tmp_path,
        )
    ]

    with pytest.raises(SessionLaunchError) as failure:
        launcher.plan(
            ProviderId.CODEX,
            ("-c", 'model_provider="unmanaged"', "prompt"),
        )

    assert failure.value.code is SessionLaunchFailure.UNSAFE_OVERRIDE

    with pytest.raises(SessionLaunchError) as nul_failure:
        launcher.plan(ProviderId.CLAUDE, ("prompt\0suffix",))

    assert nul_failure.value.code is SessionLaunchFailure.INVALID_ARGUMENT


def test_shell_enrollment_round_trips_bash_and_fish_without_foreign_edits(
    tmp_path: Path,
) -> None:
    """Enrollment must be idempotent, reversible, and owner-bounded."""
    home = tmp_path / "home"
    home.mkdir()
    bashrc = home / ".bashrc"
    bashrc.write_text(_ORIGINAL_BASH)
    integration = (
        home
        / ".local"
        / "share"
        / "sidekick-usages"
        / ("shell-integration.sh")
    )
    bash = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )

    first = bash.install(ShellKind.BASH, dry_run=False)
    second = bash.install(ShellKind.BASH, dry_run=False)

    assert first.changed is True
    assert second.changed is False
    assert integration.read_text() == _POSIX_FUNCTIONS
    assert integration.stat().st_mode & 0o777 == _PRIVATE_FILE_MODE
    assert bash.uninstall(ShellKind.BASH, dry_run=False).changed is True
    assert bashrc.read_text() == _ORIGINAL_BASH
    assert not integration.exists()

    fish_config = home / ".config" / "fish"
    fish_config.mkdir(parents=True)
    foreign = fish_config / "config.fish"
    foreign.write_text("set -gx EDITOR vim\n")
    fish = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/usr/bin/fish"},
            platform="darwin",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )

    installed = fish.install(ShellKind.FISH, dry_run=False)
    fish_path = fish_config / "conf.d" / "sidekick-usages.fish"

    assert installed.changed is True
    assert fish_path.read_text() == _FISH_FUNCTIONS
    assert fish.uninstall(ShellKind.FISH, dry_run=False).changed is True
    assert foreign.read_text() == "set -gx EDITOR vim\n"


def test_shell_dry_run_has_no_side_effect_and_changed_markers_fail_closed(
    tmp_path: Path,
) -> None:
    """A preview never writes, while altered ownership stays untouched."""
    home = tmp_path / "home"
    home.mkdir()
    bashrc = home / ".bashrc"
    integration = home / "absent" / "shell-integration.sh"
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )

    preview = shell.install(ShellKind.BASH, dry_run=True)

    assert preview.changed is True
    assert preview.paths == (bashrc, integration)
    assert preview.diffs
    assert not bashrc.exists()
    assert not integration.parent.exists()

    bashrc.write_text(
        "# >>> sidekick-usages session >>>\n"
        ". '/changed/location.sh'\n"
        "# <<< sidekick-usages session <<<\n"
    )
    with pytest.raises(ShellIntegrationError) as failure:
        shell.uninstall(ShellKind.BASH, dry_run=False)

    assert failure.value.code is ShellIntegrationFailure.SOURCE_CHANGED
    assert failure.value.manual_range == (1, 3)
    assert bashrc.read_text() == (
        "# >>> sidekick-usages session >>>\n"
        ". '/changed/location.sh'\n"
        "# <<< sidekick-usages session <<<\n"
    )


def test_public_shell_dry_run_reports_exact_targets_without_writing(
    tmp_path: Path,
) -> None:
    """The public preview must expose its qualified plan without mutation."""
    home = tmp_path / "home"
    home.mkdir()
    integration = (
        home
        / ".local"
        / "share"
        / "sidekick-usages"
        / ("shell-integration.sh")
    )
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )
    context = InvocationContext(
        console=Console(width=200),
        session_composer=lambda: SessionContext(shell=shell),
    )

    result = CliRunner().invoke(
        create_app(),
        ["session", "shell", "install", "--shell", "bash", "--dry-run"],
        obj=context,
        terminal_width=160,
    )

    assert result.exit_code == ExitCode.SUCCESS
    output = click.unstyle(result.output)
    assert "Dry run" in output
    assert str(home / ".bashrc") in output
    assert str(integration) in output
    assert "stable bounded reads" in output
    assert not (home / ".bashrc").exists()
    assert not integration.parent.exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_provider_session_commands_refuse_unavailable_capabilities(
    provider: str,
) -> None:
    """Provider entrypoints must not fall through to unmanaged processes."""
    result = CliRunner().invoke(
        create_app(),
        ["session", provider, "--", "prompt with spaces"],
        terminal_width=160,
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = click.unstyle(result.output)
    assert f"{provider} session integration is not available" in output
    assert "provider process was not" in output
    assert "started." in output
