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
import textwrap
from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread
from typing import Never

import click
import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts.session import SessionContext
from sidekick_usages.cli.session.codex import (
    CodexCliSession,
    CodexSessionRuntime,
)
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
from sidekick_usages.daemon.control import dispatch, protocol
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.posix.adapter import PosixPlatform
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.shell import (
    ShellFileStore,
    ShellPersistenceError,
)
from sidekick_usages.persistence.supervisor import selection
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.structured import process
from sidekick_usages.providers.codex.app_server import executable
from sidekick_usages.providers.codex.session import quiescence
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
    write_fake_managed_codex,
    write_resident_session_config,
)
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.daemon.foundation import foundation_state
from tests.support.time import FixedClock

_ORIGINAL_BASH = "# user alias\nalias ll='ls -l'\n"
_SIDEKICK_EXECUTABLE = ExecutableProvenance(
    Path("/opt/sidekick usages/bin/sidekick-usages"), 1, 1, 1, 1
)
_POSIX_FUNCTIONS = """claude() {
    command '/opt/sidekick usages/bin/sidekick-usages' session claude -- "$@"
}
codex() {
    command '/opt/sidekick usages/bin/sidekick-usages' session codex -- "$@"
}
"""
_FISH_FUNCTIONS = """function claude
    command '/opt/sidekick usages/bin/sidekick-usages' session claude -- $argv
end
function codex
    command '/opt/sidekick usages/bin/sidekick-usages' session codex -- $argv
end
"""
_PRIVATE_FILE_MODE = 0o600
_PROVIDER_EXIT_CODE = 17


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


def test_claude_arguments_preserve_continuation_and_close_auth_bypass() -> (
    None
):
    """Continuation stays integrated while auth mutation fails closed."""
    conversation = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert all(
        process.claude_structured_arguments_supported(arguments)
        for arguments in ((), ("-c",), ("--resume", conversation))
    )
    assert not process.claude_structured_arguments_supported(("--resume",))
    assert process.claude_arguments_mutate_auth(("/login",))
    assert process.claude_arguments_mutate_auth(("auth", "logout"))


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
    """Enrollment must pin, upgrade, round-trip, and preserve foreign bytes."""
    home = tmp_path / "home"
    home.mkdir()
    bashrc = home / ".bashrc"
    bashrc.write_text(_ORIGINAL_BASH)
    integration = home / ".local/share/sidekick-usages/shell-integration.sh"
    integration.parent.mkdir(parents=True)
    previous = _POSIX_FUNCTIONS.replace("sidekick-usages'", "previous'")
    integration.write_text(previous)
    integration.chmod(_PRIVATE_FILE_MODE)
    bash = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        ),
        _SIDEKICK_EXECUTABLE,
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
        ),
        _SIDEKICK_EXECUTABLE,
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
        ),
        _SIDEKICK_EXECUTABLE,
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
        ),
        _SIDEKICK_EXECUTABLE,
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
    integration = home / ".local/share/sidekick-usages/shell-integration.sh"
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        ),
        _SIDEKICK_EXECUTABLE,
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
    integration = home / ".local/share/sidekick-usages/shell-integration.sh"
    shell = ShellEnrollment(
        ShellStartupResolver(
            environment={"HOME": str(home), "SHELL": "/bin/bash"},
            platform="linux",
            posix_integration=integration,
            effective_user_id=os.geteuid(),
        ),
        _SIDEKICK_EXECUTABLE,
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


def _stopped_restore_patch() -> pytest.MonkeyPatch:
    patch = pytest.MonkeyPatch()
    qualified_tcsetpgrp = os.tcsetpgrp
    terminal_calls = 0

    def fail_stopped_restore(descriptor: int, process_group: int) -> None:
        nonlocal terminal_calls
        terminal_calls += 1
        if terminal_calls > 1:
            patch.undo()
            raise OSError(errno.EIO, "synthetic terminal restore failure")
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
    stopped_context: InvocationContext,
    command_result: Path,
    child_observation: Path,
    ready_read: int,
    ready_write: int,
    daemon: FakeCodexDaemon,
    control: LocalControlServer,
    control_threads: tuple[Thread, ...],
) -> int:
    setup_read, setup_write = os.pipe()
    outer_process, terminal = pty.fork()
    if outer_process == 0:
        os.close(ready_read)
        os.close(setup_write)
        os.read(setup_read, 1)
        os.close(setup_read)
        result = CliRunner().invoke(
            create_app(),
            ["session", "codex", "--", "prompt with spaces"],
            obj=context,
        )
        restored = os.tcgetpgrp(0) == os.getpgrp()
        command_result.write_text(
            f"exit={result.exit_code}\nrestored={restored}\n"
            f"output={click.unstyle(result.output)}"
        )
        patch = _stopped_restore_patch()
        stopped = CliRunner().invoke(
            create_app(),
            ["session", "codex", "--", "stop-after-ready"],
            obj=stopped_context,
        )
        patch.undo()
        process_line = next(
            line
            for line in child_observation.read_text().splitlines()
            if line.startswith("pid=")
        )
        process_id = int(process_line.removeprefix("pid="))
        try:
            os.waitpid(process_id, os.WNOHANG)
            reaped = False
        except ChildProcessError:
            reaped = True
        stopped_output = click.unstyle(stopped.output)
        stopped_ok = (
            stopped.exit_code == 1
            and os.tcgetpgrp(0) == os.getpgrp()
            and reaped
            and "original terminal could not be restored" in stopped_output
            and "status 23." in stopped_output
            and child_observation.read_text().endswith(
                "continued=True\nnatural_exit=23\n"
            )
        )
        os.close(ready_write)
        os._exit(0 if stopped_ok else 1)
    os.close(setup_read)
    daemon.__enter__()
    try:
        control.open()
        for thread in control_threads:
            thread.start()
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
        os.kill(outer_process, signal.SIGWINCH)
        continued = _read_pty_event(
            ready_read,
            outer_process,
            terminal,
            failure="The stock Codex TUI did not continue after readiness.",
        )
        assert continued == b"2"
        fcntl.ioctl(
            terminal,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 40, 120, 0, 0),
        )
        _waited, status = os.waitpid(outer_process, 0)
        os.close(terminal)
        return os.waitstatus_to_exitcode(status)
    finally:
        daemon.__exit__(None, None, None)


@pytest.mark.skipif(
    os.name != "posix"
    or not hasattr(os, "fork")
    or os.execve not in os.supports_fd,
    reason="The stock Codex TUI relay requires a POSIX child process.",
)
def test_codex_session_runs_one_coordinated_stock_tui(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    binaries = tmp_path / "bin"
    working_directory = tmp_path / "working"
    neutral_home = short_socket_root / "codex"
    schema_root = tmp_path / "schema"
    child_observation = tmp_path / "child-observation.txt"
    command_result = tmp_path / "command-result.txt"
    binaries.mkdir()
    working_directory.mkdir()
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(
        binaries,
        schema_root,
        neutral_home,
        version="0.146.0",
    )
    codex = binaries / "codex"
    resident_codex = binaries / "resident-codex"
    codex.replace(resident_codex)
    sidekick = binaries / "sidekick-real"
    codex.write_text(
        textwrap.dedent(
            r"""
            #!__PYTHON__
            import os, signal, sys
            from pathlib import Path
            from websockets.sync.client import unix_connect
            resident = Path(__RESIDENT__)
            if sys.argv[1:] == ["--version"] or "app-server" in sys.argv[1:]:
                os.execv(resident, (str(resident), *sys.argv[1:]))
            observation = Path(os.environ["SESSION_OBSERVATION"])
            ready = int(os.environ["SESSION_READY_FD"])
            signal_count = 0
            def resized(_number, _frame):
                global signal_count
                signal_count += 1
                size = os.get_terminal_size(0)
                label = "signal" if signal_count == 1 else "resize"
                with observation.open("a") as stream:
                    stream.write(f"{label}={size.columns}x{size.lines}\n")
                if signal_count == 1:
                    os.write(ready, b"2")
                    return
                raise SystemExit(125)
            signal.signal(signal.SIGWINCH, resized)
            remote = sys.argv[sys.argv.index("--remote") + 1]
            with unix_connect(
                remote.removeprefix("unix://"), uri="ws://localhost/rpc"
            ) as connection:
                connection.send(os.environ["SESSION_INITIALIZE"])
                connection.recv()
                connection.send('{"method":"initialized"}')
                connection.send(os.environ["SESSION_TURN"])
                messages = [connection.recv() for _index in range(3)]
                assert "turn/completed" in messages[-1]
            size = os.get_terminal_size(0)
            lines = [f"arg={item}" for item in sys.argv[1:]]
            lines.extend(
                (
                    f"home={os.environ['CODEX_HOME']}", f"cwd={os.getcwd()}",
                    f"tty={os.isatty(0)}",
                    f"foreground={os.tcgetpgrp(0) == os.getpgrp()}",
                    f"size={size.columns}x{size.lines}", "turn=completed",
                )
            )
            stopped = "stop-after-ready" in sys.argv
            if stopped:
                lines.insert(0, f"pid={os.getpid()}")
            with observation.open("a" if stopped else "w") as stream:
                stream.write("\n".join(lines) + "\n")
            os.write(ready, b"1")
            if stopped:
                os.kill(os.getpid(), signal.SIGTSTP)
                observation.write_text(
                    observation.read_text()
                    + "continued=True\nnatural_exit=23\n"
                )
                os.write(ready, b"2")
                raise SystemExit(23)
            while True:
                signal.pause()
            """
        )
        .lstrip()
        .replace("__PYTHON__", sys.executable, 1)
        .replace("__RESIDENT__", repr(str(resident_codex)), 1)
    )
    codex.chmod(0o700)
    _executable(sidekick)
    ready_read, ready_write = os.pipe()
    os.set_inheritable(ready_write, True)
    state = foundation_state(short_socket_root / "state")
    participants = ParticipantRegistry(
        state.selected,
        attachments=quiescence.CodexParticipantProofSet(
            protocol.FramedTransport
        ),
    )
    clock = FixedClock()
    coordinator = SelectionCoordinator(
        state.selected,
        selection.SelectionOperationStore(state.paths.selection_journals),
        participants,
        SelectionWorkerGateway(state.queue, clock, Event().set),
        clock,
    )
    control = LocalControlServer(
        state.paths.runtime_directory,
        state.paths.supervisor_socket,
        dispatch.SupervisorDispatcher(
            state.queue,
            ServiceStateStore(state.paths.service_state),
            dispatch.OperationEventHub(),
            clock,
            Event().set,
            Event().set,
            selection=coordinator,
        ),
    )
    control_threads = tuple(
        Thread(target=control.serve_once) for _index in range(4)
    )
    participant_socket = state.paths.participant_sockets / "cli.sock"
    environment = {
        "HOME": str(tmp_path),
        "PATH": str(binaries),
        "SESSION_OBSERVATION": str(child_observation),
        "SESSION_READY_FD": str(ready_write),
        "SESSION_INITIALIZE": (
            '{"id":1,"method":"initialize","params":{"capabilities":'
            '{"experimentalApi":true},"clientInfo":{"name":"codex-tui"}}}'
        ),
        "SESSION_TURN": '{"id":10,"method":"turn/start","params":{"input":[],'
        '"threadId":"thread-cli"}}',
        "TERM": "xterm-256color",
    }
    launcher = ProviderSessionLauncher(
        environment,
        working_directory=working_directory,
        sidekick_executable=qualify_executable(sidekick),
    )
    codex_binary = executable.discover_codex_executable(environment)
    daemon = FakeCodexDaemon(
        neutral_home,
        app_server_version="0.146.0",
    )
    lifecycle = configure_codex_daemon_lifecycle(
        binaries,
        neutral_home,
        daemon.socket_path,
        app_server_version="0.146.0",
        already_running=True,
    )
    write_resident_session_config(
        neutral_home,
        model_provider="sidekick-chatgpt-http",
    )
    sessions = (
        SessionContext(
            shell=ShellEnrollment(
                ShellStartupResolver(
                    environment={},
                    platform="linux",
                    posix_integration=tmp_path / "unneeded",
                    effective_user_id=os.geteuid(),
                ),
                qualify_executable(sidekick),
            ),
            codex=CodexCliSession(
                launcher,
                CodexSessionRuntime.create(
                    codex_binary,
                    neutral_home,
                    participant_socket,
                    state.paths.supervisor_socket,
                    environment=environment,
                ),
                codex_home=neutral_home,
            ),
        )
        for _index in range(2)
    )
    contexts = tuple(
        InvocationContext(session_composer=sessions.__next__) for _ in range(2)
    )
    assert (
        _invoke_codex_in_pty(
            contexts[0],
            contexts[1],
            command_result,
            child_observation,
            ready_read,
            ready_write,
            daemon,
            control,
            control_threads,
        )
        == 0
    )
    assert daemon.relay_start_request_ids == (10, 10)
    assert command_result.read_text() == "exit=125\nrestored=True\noutput="
    observation = child_observation.read_text()
    assert all(
        item in observation
        for item in (
            f"arg=--remote\narg=unix://{participant_socket}",
            "arg=prompt with spaces",
            "size=80x24\nturn=completed\nsignal=80x24\nresize=120x40",
        )
    )
    os.close(ready_read)
    os.close(ready_write)
    for thread in control_threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    control.close()
    snapshot = participants.snapshot(ProviderId.CODEX)
    assert (
        snapshot.registered_count,
        snapshot.reachable_count,
        snapshot.active_turn_count,
        participant_socket.exists(),
        lifecycle.start_statuses,
        lifecycle.restart_count,
    ) == (2, 0, 0, False, (), 0)
