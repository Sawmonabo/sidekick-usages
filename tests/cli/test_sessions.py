"""Public session enrollment and capability-refusal behavior."""

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import click
import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts.session import SessionContext
from sidekick_usages.cli.session.codex import CodexCliSession
from sidekick_usages.cli.session.launcher import ProviderSessionLauncher
from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
    SessionLaunchSpec,
    ShellIntegrationError,
    ShellIntegrationFailure,
    ShellKind,
)
from sidekick_usages.cli.session.shell import (
    ShellEnrollment,
    ShellStartupResolver,
)
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.posix.adapter import PosixPlatform
from sidekick_usages.persistence.platform.types import NativeFailureKind
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
_PROVIDER_EXIT_CODE = 17
_RUNTIME_EVENTS = (
    "relay_open\nparticipant_registered\nnotice_subscribed\n"
    "downstream=1\nupstream=1\nsession_closed\n"
)


def _executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)


def _run_planned_session_in_child(
    launcher: ProviderSessionLauncher,
    spec: SessionLaunchSpec,
) -> int:
    process_id = os.fork()
    if process_id == 0:
        try:
            launcher.run(spec)
        except BaseException:
            os._exit(125)
        os._exit(126)
    _waited, process_status = os.waitpid(process_id, 0)
    return os.waitstatus_to_exitcode(process_status)


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
    claude.hardlink_to(sidekick)
    with pytest.raises(SessionLaunchError) as recursion:
        launcher.plan(ProviderId.CLAUDE, ())

    assert recursion.value.code is SessionLaunchFailure.RECURSIVE_EXECUTABLE


def test_launcher_executes_exact_descriptor_from_requested_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path replacement or pathname exec would break the frozen launch."""
    binaries = tmp_path / "bin"
    working_directory = tmp_path / "working"
    binaries.mkdir()
    working_directory.mkdir()
    claude = binaries / "claude"
    sidekick = binaries / "sidekick-real"
    claude.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n%s\\n\' "$1" "$SESSION_TEST" > observed.txt\n'
        f"exit {_PROVIDER_EXIT_CODE}\n"
    )
    claude.chmod(0o700)
    _executable(sidekick)

    def resolve_claude_fixture(_environment: Mapping[str, str]) -> Path:
        return claude

    launcher = ProviderSessionLauncher(
        {"PATH": str(binaries), "SESSION_TEST": "preserved"},
        working_directory=working_directory,
        sidekick_executable=qualify_executable(sidekick),
        claude_resolver=resolve_claude_fixture,
    )
    spec = launcher.plan(ProviderId.CLAUDE, ("prompt with spaces",))
    original_directory = Path.cwd()

    if hasattr(os, "fork") and os.execve in os.supports_fd:
        assert (
            _run_planned_session_in_child(launcher, spec)
            == _PROVIDER_EXIT_CODE
        )
        assert (working_directory / "observed.txt").read_text() == (
            "prompt with spaces\npreserved\n"
        )

    monkeypatch.setattr(
        "sidekick_usages.cli.session.launcher._DESCRIPTOR_EXEC_SUPPORTED",
        False,
    )
    with pytest.raises(SessionLaunchError) as unsupported:
        launcher.run(spec)

    assert unsupported.value.code is SessionLaunchFailure.UNSUPPORTED
    if os.name != "posix":
        return

    calls: list[
        tuple[int, os.stat_result, tuple[str, ...], dict[str, str], Path]
    ] = []

    def reject_after_observation(
        descriptor: int,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> int:
        calls.append(
            (
                descriptor,
                os.fstat(descriptor),
                arguments,
                environment,
                Path.cwd(),
            )
        )
        raise OSError("Synthetic descriptor exec refusal.")

    monkeypatch.setattr(
        "sidekick_usages.cli.session.launcher.os.execve",
        reject_after_observation,
    )
    monkeypatch.setattr(
        "sidekick_usages.cli.session.launcher._DESCRIPTOR_EXEC_SUPPORTED",
        True,
    )

    with pytest.raises(SessionLaunchError) as failure:
        launcher.run(spec)

    assert failure.value.code is SessionLaunchFailure.EXECUTION_FAILED
    assert Path.cwd() == original_directory
    descriptor, metadata, arguments, environment, executed_from = calls[0]
    assert (metadata.st_dev, metadata.st_ino) == (
        spec.executable.device,
        spec.executable.inode,
    )
    assert arguments == (str(claude), "prompt with spaces")
    assert environment["SESSION_TEST"] == "preserved"
    assert executed_from == working_directory
    with pytest.raises(OSError, match=os.strerror(errno.EBADF)):
        os.fstat(descriptor)

    replacement = binaries / "replacement-claude"
    _executable(replacement)
    replacement.replace(claude)
    with pytest.raises(SessionLaunchError) as changed:
        launcher.run(spec)

    assert changed.value.code is SessionLaunchFailure.EXECUTABLE_CHANGED
    assert len(calls) == 1


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


def test_zsh_zdotdir_round_trips_a_file_without_a_final_newline(
    tmp_path: Path,
) -> None:
    """Ignoring ZDOTDIR or an inserted separator would alter user bytes."""
    home = tmp_path / "home"
    zdotdir = tmp_path / "zsh-config"
    home.mkdir()
    zdotdir.mkdir()
    zshrc = zdotdir / ".zshrc"
    original = b"export EDITOR=vim"
    zshrc.write_bytes(original)
    integration = home / ".local" / "share" / "sidekick" / "session.sh"
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={
                "HOME": str(home),
                "SHELL": "/bin/zsh",
                "ZDOTDIR": str(zdotdir),
            },
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )

    installed = shell.install(ShellKind.ZSH, dry_run=False)

    assert installed.paths == (zshrc, integration)
    assert shell.status(ShellKind.ZSH).state.value == "integrated"
    assert shell.uninstall(ShellKind.ZSH, dry_run=False).changed is True
    assert zshrc.read_bytes() == original
    assert not integration.exists()


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
    assert inverted.value.manual_range is None
    assert "ambiguous" in str(inverted.value)

    bashrc.write_text(
        "echo foreign\n# >>> sidekick-usages session >>>\necho unrelated\n"
    )
    with pytest.raises(ShellIntegrationError) as orphan:
        shell.uninstall(ShellKind.BASH, dry_run=False)

    assert orphan.value.code is ShellIntegrationFailure.SOURCE_CHANGED
    assert orphan.value.manual_range == (2, 2)


def test_shell_persistence_refuses_an_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """Following one Linux path component could mutate an outside tree."""
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    integration = (
        home / ".local" / "share" / "sidekick-usages" / "shell-integration.sh"
    )
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        )
    )

    with pytest.raises(ShellIntegrationError) as failure:
        shell.install(ShellKind.BASH, dry_run=True)

    assert failure.value.code is ShellIntegrationFailure.UNSAFE_PATH
    assert not (outside / "share").exists()


def test_shell_remove_refuses_replacement_before_native_validation(
    tmp_path: Path,
) -> None:
    """A replacement before native validation must remain untouched."""
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
            root: Path,
            parent: Path,
            basename: str,
            device: int,
            inode: int,
        ) -> bool:
            (parent / basename).rename(survivor)
            replacement.rename(parent / basename)
            return super().remove_shell_validated(
                root,
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


@pytest.mark.parametrize(
    ("failure_target", "primary_kind"),
    [
        ("_write_temporary", NativeFailureKind.WRITE),
        ("_publish_temporary", NativeFailureKind.CHANGED),
    ],
)
def test_shell_write_preserves_primary_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    primary_kind: NativeFailureKind,
) -> None:
    """Temporary cleanup must not replace a meaningful write failure."""

    def fail_primary(
        *_arguments: object,
        **_keywords: object,
    ) -> Never:
        raise NativeFilesystemError(primary_kind)

    def fail_cleanup(
        *_arguments: object,
        **_keywords: object,
    ) -> Never:
        raise NativeFilesystemError(NativeFailureKind.REMOVE)

    owner = "sidekick_usages.persistence.platform.posix.shell"
    monkeypatch.setattr(f"{owner}.{failure_target}", fail_primary)
    monkeypatch.setattr(f"{owner}._remove_temporary", fail_cleanup)
    store = ShellFileStore(tmp_path, os.geteuid())

    with pytest.raises(ShellPersistenceError) as failure:
        store.write(
            tmp_path / "session.sh",
            None,
            _POSIX_FUNCTIONS.encode(),
            owner_only=True,
        )

    primary = failure.value.__cause__
    assert isinstance(primary, NativeFilesystemError)
    assert primary.kind is primary_kind
    assert primary.__notes__ == ["Temporary shell cleanup also failed."]


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


def test_claude_session_command_refuses_unavailable_capability() -> None:
    """Provider entrypoints must not fall through to unmanaged processes."""
    result = CliRunner().invoke(
        create_app(),
        ["session", "claude", "--", "prompt with spaces"],
        terminal_width=160,
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = click.unstyle(result.output)
    assert "claude session integration is not available" in output
    assert "provider process was not" in output
    assert "started." in output


@dataclass(slots=True)
class _FakeCodexRuntime:
    """Expose one stable synthetic relay and participant lifetime."""

    socket_path: Path
    ready_marker: Path
    child_observation: Path
    event_log: Path
    executable_replacement: tuple[Path, Path] | None = None

    def open(self) -> None:
        """Open one relay before registration and account-bearing traffic."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.event_log.open("a") as stream:
            stream.write(
                "relay_open\nparticipant_registered\n"
                "notice_subscribed\ndownstream=1\nupstream=1\n"
            )
        self.ready_marker.write_text("ready\n")
        if self.executable_replacement is not None:
            replacement, target = self.executable_replacement
            replacement.replace(target)
            self.executable_replacement = None

    def close(self) -> None:
        """Close only after the one child has exited naturally."""
        if not self.child_observation.exists():
            raise AssertionError("Codex runtime closed before its child.")
        with self.event_log.open("a") as stream:
            stream.write("session_closed\n")
        self.ready_marker.unlink()


def _stopped_restore_patch() -> pytest.MonkeyPatch:
    patch = pytest.MonkeyPatch()
    qualified_tcsetpgrp = os.tcsetpgrp
    terminal_calls = 0

    def fail_stopped_restore(
        descriptor: int,
        process_group: int,
    ) -> None:
        nonlocal terminal_calls
        terminal_calls += 1
        if terminal_calls > 1:
            patch.undo()
            raise OSError(
                errno.EIO,
                "synthetic stopped-child restoration failure",
            )
        qualified_tcsetpgrp(descriptor, process_group)

    patch.setattr(os, "tcsetpgrp", fail_stopped_restore)
    return patch


def _read_pty_event(
    descriptor: int,
    outer_process: int,
    terminal: int,
    *,
    failure: str,
) -> bytes:
    if select.select([descriptor], [], [], 5)[0]:
        return os.read(descriptor, 1)
    os.kill(outer_process, signal.SIGTERM)
    os.waitpid(outer_process, 0)
    os.close(descriptor)
    os.close(terminal)
    pytest.fail(failure)


def _invoke_codex_in_pty(
    context: InvocationContext,
    command_result: Path,
    ready_read: int,
    ready_write: int,
    *,
    stopped_restore_failure: bool = False,
    child_observation: Path | None = None,
) -> int:
    setup_read, setup_write = os.pipe()
    outer_process, terminal = pty.fork()
    if outer_process == 0:
        os.close(ready_read)
        os.close(setup_write)
        os.read(setup_read, 1)
        os.close(setup_read)
        terminal_patch = (
            _stopped_restore_patch()
            if stopped_restore_failure
            else pytest.MonkeyPatch()
        )
        argument = (
            "stop-after-ready"
            if stopped_restore_failure
            else "prompt with spaces"
        )
        result = CliRunner().invoke(
            create_app(),
            ["session", "codex", "--", argument],
            obj=context,
            terminal_width=160,
        )
        terminal_patch.undo()
        restored = os.tcgetpgrp(0) == os.getpgrp()
        reaped = ""
        if child_observation is not None:
            process_line = child_observation.read_text().splitlines()[0]
            provider_process = int(process_line.removeprefix("pid="))
            with pytest.raises(ChildProcessError):
                os.waitpid(provider_process, os.WNOHANG)
            reaped = "reaped=True\n"
        command_result.write_text(
            f"exit={result.exit_code}\nrestored={restored}\n"
            f"{reaped}output={click.unstyle(result.output)}"
        )
        os.close(ready_write)
        os._exit(0)
    os.close(setup_read)
    fcntl.ioctl(
        terminal,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 80, 0, 0),
    )
    os.write(setup_write, b"1")
    os.close(setup_write)
    ready = _read_pty_event(
        ready_read,
        outer_process,
        terminal,
        failure="The stock Codex TUI did not reach terminal readiness.",
    )
    assert ready == b"1"
    if not stopped_restore_failure:
        os.kill(outer_process, signal.SIGWINCH)
    continued = _read_pty_event(
        ready_read,
        outer_process,
        terminal,
        failure="The stock Codex TUI did not continue after readiness.",
    )
    assert continued == b"2"
    if not stopped_restore_failure:
        fcntl.ioctl(
            terminal,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 40, 120, 0, 0),
        )
    _waited, status = os.waitpid(outer_process, 0)
    os.close(terminal)
    return os.waitstatus_to_exitcode(status)


def _prove_stopped_restore_failure(
    context: InvocationContext,
    result_path: Path,
    observation: Path,
    ready_read: int,
    ready_write: int,
) -> None:
    status = _invoke_codex_in_pty(
        context,
        result_path,
        ready_read,
        ready_write,
        stopped_restore_failure=True,
        child_observation=observation,
    )
    assert status == 0
    result = result_path.read_text().splitlines()
    assert "\n".join(result[:3]) == "exit=1\nrestored=True\nreaped=True"
    assert "original terminal could not be restored" in result[3]
    assert result[4] == "status 23."
    assert observation.read_text().endswith(
        "continued=True\nnatural_exit=23\n"
    )


@pytest.mark.skipif(
    os.name != "posix"
    or not hasattr(os, "fork")
    or os.execve not in os.supports_fd,
    reason="The stock Codex TUI relay requires a POSIX child process.",
)
def test_codex_session_runs_one_coordinated_stock_tui(
    tmp_path: Path,
) -> None:
    """One retained TUI must preserve its terminal and launch contract."""
    binaries = tmp_path / "bin"
    working_directory = tmp_path / "working"
    neutral_home = tmp_path / "neutral-codex-home"
    participant_socket = tmp_path / "runtime" / "participants" / "cli.sock"
    child_observation = tmp_path / "child-observation.txt"
    command_result = tmp_path / "command-result.txt"
    event_log = tmp_path / "runtime-events.txt"
    ready_marker = tmp_path / "relay-ready.txt"
    binaries.mkdir()
    working_directory.mkdir()
    neutral_home.mkdir(mode=0o700)
    codex = binaries / "codex"
    sidekick = binaries / "sidekick-real"
    codex.write_text(
        f"#!{sys.executable}\n"
        "import os\nimport signal\nimport sys\nfrom pathlib import Path\n"
        "observation = Path(os.environ['SESSION_OBSERVATION'])\n"
        "ready = int(os.environ['SESSION_READY_FD'])\nsignal_count = 0\n"
        "def resized(_number, _frame):\n"
        "    global signal_count\n    signal_count += 1\n"
        "    size = os.get_terminal_size(0)\n"
        "    with observation.open('a') as stream:\n"
        "        label = 'signal' if signal_count == 1 else 'resize'\n"
        "        stream.write(f'{label}={size.columns}x{size.lines}\\n')\n"
        "    if signal_count == 1:\n"
        "        os.write(ready, b'2')\n        return\n"
        "    raise SystemExit(125)\n"
        "signal.signal(signal.SIGWINCH, resized)\n"
        "size = os.get_terminal_size(0)\n"
        "lines = [f'arg={item}' for item in sys.argv[1:]]\n"
        "lines.extend([\n"
        "    f\"home={os.environ['CODEX_HOME']}\",\n"
        "    f\"cwd={os.getcwd()}\",\n    f'tty={os.isatty(0)}',\n"
        "    f'foreground={os.tcgetpgrp(0) == os.getpgrp()}',\n"
        "    f'size={size.columns}x{size.lines}',\n"
        "])\n"
        "if 'stop-after-ready' in sys.argv:\n"
        "    lines.insert(0, f'pid={os.getpid()}')\n"
        "observation.write_text('\\n'.join(lines) + '\\n')\n"
        "if not Path(os.environ['SESSION_READY']).is_file():\n"
        "    raise SystemExit(91)\n"
        "os.write(ready, b'1')\n"
        "if 'stop-after-ready' in sys.argv:\n"
        "    os.kill(os.getpid(), signal.SIGTSTP)\n"
        "    with observation.open('a') as stream:\n"
        "        stream.write('continued=True\\nnatural_exit=23\\n')\n"
        "    os.write(ready, b'2')\n    raise SystemExit(23)\n"
        "while True:\n    signal.pause()\n"
    )
    codex.chmod(0o700)
    _executable(sidekick)
    ready_read, ready_write = os.pipe()
    os.set_inheritable(ready_write, True)
    runtime = _FakeCodexRuntime(
        participant_socket,
        ready_marker,
        child_observation,
        event_log,
    )
    launcher = ProviderSessionLauncher(
        {
            "PATH": str(binaries),
            "SESSION_OBSERVATION": str(child_observation),
            "SESSION_READY": str(ready_marker),
            "SESSION_READY_FD": str(ready_write),
            "TERM": "xterm-256color",
        },
        working_directory=working_directory,
        sidekick_executable=qualify_executable(sidekick),
    )
    context = InvocationContext(
        session_composer=lambda: SessionContext(
            shell=ShellEnrollment(
                ShellStartupResolver(
                    environment={},
                    platform="linux",
                    posix_integration=tmp_path / "unneeded",
                    effective_user_id=os.geteuid(),
                )
            ),
            codex=CodexCliSession(
                launcher,
                runtime,
                codex_home=neutral_home,
            ),
        )
    )
    status = _invoke_codex_in_pty(
        context,
        command_result,
        ready_read,
        ready_write,
    )

    assert status == 0
    assert command_result.read_text() == "exit=125\nrestored=True\noutput="
    assert child_observation.read_text().splitlines() == [
        "arg=--remote",
        f"arg=unix://{participant_socket}",
        "arg=prompt with spaces",
        f"home={neutral_home}",
        f"cwd={working_directory}",
        "tty=True",
        "foreground=True",
        "size=80x24",
        "signal=80x24",
        "resize=120x40",
    ]

    _prove_stopped_restore_failure(
        context,
        tmp_path / "stopped-result.txt",
        child_observation,
        ready_read,
        ready_write,
    )
    os.close(ready_read)
    os.close(ready_write)

    unsafe = CliRunner().invoke(
        create_app(),
        [
            "session",
            "codex",
            "--",
            "--remote",
            "unix:///unmanaged.sock",
        ],
        obj=context,
        terminal_width=160,
    )

    assert unsafe.exit_code == ExitCode.MANUAL_ACTION
    assert "Codex arguments override protected" in click.unstyle(unsafe.output)
    assert event_log.read_text() == _RUNTIME_EVENTS * 2

    replacement = binaries / "replacement-codex"
    _executable(replacement)
    runtime.executable_replacement = replacement, codex
    changed = CliRunner().invoke(
        create_app(),
        ["session", "codex", "--", "another prompt"],
        obj=context,
        terminal_width=160,
    )

    assert changed.exit_code == ExitCode.MANUAL_ACTION
    assert "changed after qualification" in click.unstyle(changed.output)
    assert child_observation.read_text().count("foreground=True") == 1
    assert event_log.read_text() == _RUNTIME_EVENTS * 3
