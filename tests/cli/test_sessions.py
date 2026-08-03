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
from sidekick_usages.persistence.platform.posix.adapter import PosixPlatform
from sidekick_usages.persistence.shell import (
    ShellFileStore,
    ShellPersistenceError,
)
from sidekick_usages.platform.executable import qualify_executable

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
    sidekick = binaries / "sidekick-real"
    _executable(claude)
    _executable(codex)
    _executable(sidekick)

    launcher = ProviderSessionLauncher(
        {"PATH": str(binaries), "TERM": "xterm-256color"},
        working_directory=tmp_path,
        sidekick_executable=qualify_executable(sidekick),
    )
    arguments = ("--model", "sonnet", "prompt with spaces")

    spec = launcher.plan(ProviderId.CLAUDE, arguments)

    assert spec.provider_arguments == arguments
    assert spec.command == (str(claude), *arguments)
    assert spec.working_directory == tmp_path

    with pytest.raises(SessionLaunchError) as failure:
        launcher.plan(
            ProviderId.CODEX,
            ("-c", 'model_provider="unmanaged"', "prompt"),
        )

    assert failure.value.code is SessionLaunchFailure.UNSAFE_OVERRIDE

    with pytest.raises(SessionLaunchError) as nul_failure:
        launcher.plan(ProviderId.CLAUDE, ("prompt\0suffix",))

    assert nul_failure.value.code is SessionLaunchFailure.INVALID_ARGUMENT

    claude.unlink()
    claude.symlink_to(sidekick)
    with pytest.raises(SessionLaunchError) as recursion:
        launcher.plan(ProviderId.CLAUDE, ())

    assert recursion.value.code is SessionLaunchFailure.RECURSIVE_EXECUTABLE


@pytest.mark.parametrize(
    "provider_arguments",
    [
        ("--model", "gpt-5", "login"),
        ("-c", "model_reasoning_effort=high", "logout"),
    ],
)
def test_codex_auth_commands_after_global_options_fail_closed(
    tmp_path: Path,
    provider_arguments: tuple[str, ...],
) -> None:
    """Global options must not hide an effective auth subcommand."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    sidekick = binaries / "sidekick-real"
    codex = binaries / "codex"
    _executable(sidekick)
    _executable(codex)
    launcher = ProviderSessionLauncher(
        {"PATH": str(binaries)},
        working_directory=tmp_path,
        sidekick_executable=qualify_executable(sidekick),
    )

    with pytest.raises(SessionLaunchError) as failure:
        launcher.plan(ProviderId.CODEX, provider_arguments)

    assert failure.value.code is SessionLaunchFailure.UNSAFE_OVERRIDE


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

    integration.chmod(0o644)
    assert bash.status(ShellKind.BASH).state.value == "bypassed"
    with pytest.raises(ShellIntegrationError) as mode_failure:
        bash.install(ShellKind.BASH, dry_run=False)
    assert mode_failure.value.code is ShellIntegrationFailure.UNSAFE_PATH
    integration.chmod(_PRIVATE_FILE_MODE)

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
    fish_path.parent.chmod(0o755)
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

    bashrc.write_text(
        "# <<< sidekick-usages session <<<\n"
        "echo foreign\n"
        "# >>> sidekick-usages session >>>\n"
    )
    with pytest.raises(ShellIntegrationError) as inverted:
        shell.uninstall(ShellKind.BASH, dry_run=False)

    assert inverted.value.code is ShellIntegrationFailure.SOURCE_CHANGED
    assert inverted.value.manual_range == (1, 3)


def test_shell_remove_rejects_injected_namespace_replacement(
    tmp_path: Path,
) -> None:
    """A replacement after stable read must survive failed removal."""
    target = tmp_path / "sidekick-usages.fish"
    survivor = tmp_path / "original-survivor.fish"
    replacement = tmp_path / "replacement.fish"
    target.write_text(_FISH_FUNCTIONS)
    target.chmod(_PRIVATE_FILE_MODE)
    replacement.write_text("function replacement\nend\n")
    replacement.chmod(_PRIVATE_FILE_MODE)

    class ReplacingPlatform(PosixPlatform):
        def remove_shell_validated(
            self,
            parent: Path,
            basename: str,
            device: int,
            inode: int,
        ) -> bool:
            (parent / basename).rename(survivor)
            replacement.rename(parent / basename)
            return super().remove_shell_validated(
                parent,
                basename,
                device,
                inode,
            )

    store = ShellFileStore(
        tmp_path,
        os.geteuid(),
        _native=ReplacingPlatform(),
    )
    snapshot = store.read(target, owner_only=True)
    assert snapshot is not None

    with pytest.raises(ShellPersistenceError):
        store.remove(target, snapshot)

    assert target.read_text() == "function replacement\nend\n"
    assert survivor.read_text() == _FISH_FUNCTIONS


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
