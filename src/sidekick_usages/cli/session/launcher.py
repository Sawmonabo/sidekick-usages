"""Exact provider executable launch planning."""

import os
import signal
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Never

from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
    SessionLaunchSpec,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.activation.service import (
    claude_environment_conflict,
)
from sidekick_usages.providers.claude.managed.executable import (
    resolve_claude_launcher,
)
from sidekick_usages.providers.codex.app_server.executable import (
    resolve_codex_launcher,
)
from sidekick_usages.providers.codex.auth.home import (
    CODEX_HOME_ENVIRONMENT_KEY,
)

type ProviderLauncherResolver = Callable[[Mapping[str, str]], Path]
type SignalHandler = Callable[[int, FrameType | None], object] | int | None

_CLAUDE_UNSAFE_OPTIONS = frozenset(
    {
        "--api-key",
        "--auth-token",
        "--bare",
        "--base-url",
        "--config-dir",
        "--credential-helper",
        "--setting-sources",
        "--settings",
        "--transport",
    }
)
_CODEX_UNSAFE_OPTIONS = frozenset(
    {
        "--api-key",
        "--base-url",
        "--home",
        "--login-with-api-key",
        "--local-provider",
        "--oss",
        "--remote",
    }
)
_CODEX_PROTECTED_CONFIG_PREFIXES = (
    "model_provider",
    "model_providers.",
    "openai_base_url",
    "wire_api",
    "requires_openai_auth",
    "supports_websockets",
    "experimental_use_websocket",
)
_CODEX_ENVIRONMENT_OVERRIDES = (
    CODEX_HOME_ENVIRONMENT_KEY,
    "CODEX_API_KEY",
    "CODEX_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)
_CODEX_AUTH_COMMANDS = frozenset({"login", "logout"})
_MAXIMUM_CODEX_ARGUMENTS = 4_096
_DESCRIPTOR_EXEC_SUPPORTED = os.execve in os.supports_fd
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_EXECUTABLE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_CHILD_EXECUTION_FAILURE = 125
_FORWARDED_SIGNALS = tuple(
    member
    for member in (
        getattr(signal, "SIGHUP", None),
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGQUIT", None),
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGWINCH", None),
    )
    if isinstance(member, signal.Signals)
)


class ProviderSessionLauncher:
    """Plan and execute one exact provider process-image replacement."""

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        working_directory: Path | None = None,
        sidekick_executable: ExecutableProvenance,
        claude_resolver: ProviderLauncherResolver = resolve_claude_launcher,
        codex_resolver: ProviderLauncherResolver = resolve_codex_launcher,
    ) -> None:
        source = os.environ if environment is None else environment
        self._environment = dict(source)
        self._working_directory = (
            Path.cwd() if working_directory is None else working_directory
        )
        self._sidekick_executable = sidekick_executable
        self._resolvers = {
            ProviderId.CLAUDE: claude_resolver,
            ProviderId.CODEX: codex_resolver,
        }

    def plan(
        self,
        provider_id: ProviderId,
        provider_arguments: tuple[str, ...],
    ) -> SessionLaunchSpec:
        """Return one exact qualified launch or a typed prelaunch refusal."""
        self._validate_common(provider_arguments)
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\0" in key
            or "\0" in value
            for key, value in self._environment.items()
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.INVALID_ARGUMENT,
                "Provider environment entries must be NUL-free strings.",
            )
        if not self._working_directory.is_absolute():
            raise SessionLaunchError(
                SessionLaunchFailure.INVALID_ARGUMENT,
                "The provider working directory must be absolute.",
            )
        if provider_id is ProviderId.CLAUDE:
            self._validate_claude(provider_arguments)
        else:
            self._validate_codex(provider_arguments)
        try:
            launcher = self._resolvers[provider_id](self._environment)
            executable = qualify_executable(launcher)
        except ExecutableQualificationError as error:
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTABLE_CHANGED,
                "The official provider executable is unavailable or unsafe.",
            ) from error
        if (executable.device, executable.inode) == (
            self._sidekick_executable.device,
            self._sidekick_executable.inode,
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.RECURSIVE_EXECUTABLE,
                "The provider launcher resolves back to Sidekick.",
            )
        return SessionLaunchSpec(
            provider_id=provider_id,
            launcher=launcher,
            executable=executable,
            provider_arguments=provider_arguments,
            environment=tuple(sorted(self._environment.items())),
            working_directory=self._working_directory,
        )

    def run(self, spec: SessionLaunchSpec) -> int:
        """Replace Sidekick with one exact descriptor-qualified provider."""
        if not _DESCRIPTOR_EXEC_SUPPORTED:
            raise SessionLaunchError(
                SessionLaunchFailure.UNSUPPORTED,
                "This platform cannot execute a qualified file descriptor.",
            )
        executable_descriptor = self._open_executable(spec.executable)
        original_directory = -1
        working_directory = -1
        changed_directory = False
        result = 0
        failure: BaseException | None = None
        try:
            original_directory = os.open(".", _DIRECTORY_OPEN_FLAGS)
            working_directory = os.open(
                spec.working_directory,
                _DIRECTORY_OPEN_FLAGS,
            )
            if not stat.S_ISDIR(os.fstat(working_directory).st_mode):
                raise OSError
            os.fchdir(working_directory)
            changed_directory = True
            result = os.execve(
                executable_descriptor,
                spec.command,
                dict(spec.environment),
            )
        except BaseException as error:
            failure = error
        _finish_launch(
            executable_descriptor,
            original_directory,
            working_directory,
            changed_directory=changed_directory,
            failure=failure,
        )
        return result

    def plan_codex_remote(
        self,
        provider_arguments: tuple[str, ...],
        *,
        socket_path: Path,
        codex_home: Path,
    ) -> SessionLaunchSpec:
        """Plan one protected stock TUI against a qualified relay."""
        if (
            not socket_path.is_absolute()
            or not codex_home.is_absolute()
            or "\0" in str(socket_path)
            or "\0" in str(codex_home)
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.INVALID_ARGUMENT,
                "Codex session paths must be absolute and NUL-free.",
            )
        planned = self.plan(ProviderId.CODEX, provider_arguments)
        environment = dict(planned.environment)
        environment[CODEX_HOME_ENVIRONMENT_KEY] = str(codex_home)
        return replace(
            planned,
            provider_arguments=(
                "--remote",
                f"unix://{socket_path}",
                *provider_arguments,
            ),
            environment=tuple(sorted(environment.items())),
        )

    def prepare_child(self, spec: SessionLaunchSpec) -> ProviderSessionChild:
        """Fork one blocked child before participant subscription starts."""
        if (
            not hasattr(os, "fork")
            or not hasattr(os, "waitpid")
            or not hasattr(os, "setpgid")
            or not _DESCRIPTOR_EXEC_SUPPORTED
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.UNSUPPORTED,
                "This platform cannot retain a qualified provider child.",
            )
        try:
            release_read, release_write = os.pipe()
        except OSError as error:
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTION_FAILED,
                "The qualified provider child could not be prepared.",
            ) from error
        try:
            process_id = os.fork()
        except OSError as error:
            with suppress(OSError):
                os.close(release_read)
            with suppress(OSError):
                os.close(release_write)
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTION_FAILED,
                "The qualified provider child could not be prepared.",
            ) from error
        if process_id == 0:
            os.close(release_write)
            try:
                os.setpgid(0, 0)
                released = os.read(release_read, 1)
                os.close(release_read)
                if released != b"1":
                    os._exit(_CHILD_EXECUTION_FAILURE)
                result = self.run(spec)
            except BaseException:
                os._exit(_CHILD_EXECUTION_FAILURE)
            os._exit(result)
        os.close(release_read)
        return ProviderSessionChild(process_id, release_write)

    @staticmethod
    def _open_executable(provenance: ExecutableProvenance) -> int:
        descriptor = -1
        try:
            descriptor = os.open(provenance.path, _EXECUTABLE_OPEN_FLAGS)
            metadata = os.fstat(descriptor)
            current = ExecutableProvenance.from_stat(
                provenance.path,
                metadata,
            )
            if (
                current != provenance
                or not stat.S_ISREG(metadata.st_mode)
                or not metadata.st_mode & 0o111
            ):
                raise OSError
            if os.pread(descriptor, 2, 0) == b"#!":
                os.set_inheritable(descriptor, True)
            return descriptor
        except OSError as error:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    error.add_note(
                        "The changed executable descriptor also failed "
                        "to close."
                    )
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTABLE_CHANGED,
                "The provider executable changed after qualification.",
            ) from error

    @staticmethod
    def _validate_common(provider_arguments: tuple[str, ...]) -> None:
        if not isinstance(provider_arguments, tuple) or any(
            not isinstance(argument, str) or "\0" in argument
            for argument in provider_arguments
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.INVALID_ARGUMENT,
                "Provider arguments must be NUL-free strings.",
            )

    def _validate_claude(
        self,
        provider_arguments: tuple[str, ...],
    ) -> None:
        if claude_environment_conflict(self._environment) is not None:
            raise SessionLaunchError(
                SessionLaunchFailure.UNSAFE_OVERRIDE,
                "The environment overrides Claude session authority.",
            )
        if any(
            _option_name(argument) in _CLAUDE_UNSAFE_OPTIONS
            for argument in provider_arguments
        ):
            raise SessionLaunchError(
                SessionLaunchFailure.UNSAFE_OVERRIDE,
                "Claude arguments override protected session authority.",
            )

    def _validate_codex(
        self,
        provider_arguments: tuple[str, ...],
    ) -> None:
        if any(
            self._environment.get(key) for key in _CODEX_ENVIRONMENT_OVERRIDES
        ) or _contains_codex_auth_command(provider_arguments):
            raise SessionLaunchError(
                SessionLaunchFailure.UNSAFE_OVERRIDE,
                "The environment overrides Codex session authority.",
            )
        for index, argument in enumerate(provider_arguments):
            option = _option_name(argument)
            if option in _CODEX_UNSAFE_OPTIONS:
                raise SessionLaunchError(
                    SessionLaunchFailure.UNSAFE_OVERRIDE,
                    "Codex arguments override protected session authority.",
                )
            config = _codex_config_value(provider_arguments, index)
            if config is not None and _protected_codex_config(config):
                raise SessionLaunchError(
                    SessionLaunchFailure.UNSAFE_OVERRIDE,
                    "Codex config overrides protected session authority.",
                )


class ProviderSessionChild:
    """Own one blocked child until relay readiness permits provider exec."""

    def __init__(self, process_id: int, release_descriptor: int) -> None:
        self._process_id = process_id
        self._release_descriptor = release_descriptor
        self._completed = False

    def run(self) -> int:
        """Release one provider child and return its natural exit status."""
        if self._completed or self._release_descriptor < 0:
            raise RuntimeError("The provider child is already complete.")
        original_group = _foreground_process_group()
        previous_handlers = _install_signal_forwarding(self._process_id)
        released = False
        try:
            with suppress(PermissionError, ProcessLookupError):
                os.setpgid(self._process_id, self._process_id)
            _set_foreground_process_group(self._process_id, original_group)
            os.write(self._release_descriptor, b"1")
            os.close(self._release_descriptor)
            self._release_descriptor = -1
            released = True
            result = _wait_for_child(self._process_id, original_group)
            self._completed = True
            return result
        finally:
            if not released:
                self._cancel_and_wait()
            _set_foreground_process_group(original_group, original_group)
            _restore_signal_handlers(previous_handlers)

    def cancel(self) -> None:
        """Reap one child without ever releasing provider execution."""
        if self._completed:
            return
        self._cancel_and_wait()

    def _cancel_and_wait(self) -> None:
        if self._release_descriptor >= 0:
            with suppress(OSError):
                os.close(self._release_descriptor)
            self._release_descriptor = -1
        if not self._completed:
            _wait_for_child(self._process_id, None)
            self._completed = True


def _preserve_cleanup_failure(
    failure: BaseException | None,
    detail: str,
) -> BaseException:
    if failure is not None:
        failure.add_note(detail)
        return failure
    return SessionLaunchError(
        SessionLaunchFailure.EXECUTION_FAILED,
        detail,
    )


def _finish_launch(
    executable_descriptor: int,
    original_directory: int,
    working_directory: int,
    *,
    changed_directory: bool,
    failure: BaseException | None,
) -> None:
    if changed_directory:
        try:
            os.fchdir(original_directory)
        except OSError:
            failure = _preserve_cleanup_failure(
                failure,
                "The original working directory could not be restored.",
            )
    for descriptor in (
        working_directory,
        original_directory,
        executable_descriptor,
    ):
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError:
            failure = _preserve_cleanup_failure(
                failure,
                "A provider launch descriptor could not be closed.",
            )
    if failure is not None:
        _raise_launch_failure(failure)


def _raise_launch_failure(failure: BaseException) -> Never:
    if isinstance(failure, SessionLaunchError):
        raise failure
    if isinstance(failure, NotImplementedError):
        raise SessionLaunchError(
            SessionLaunchFailure.UNSUPPORTED,
            "This platform refused descriptor execution.",
        ) from failure
    if isinstance(failure, OSError):
        raise SessionLaunchError(
            SessionLaunchFailure.EXECUTION_FAILED,
            "The qualified provider process could not start.",
        ) from failure
    raise failure


def _option_name(argument: str) -> str:
    return argument.partition("=")[0]


def _codex_config_value(
    arguments: tuple[str, ...],
    index: int,
) -> str | None:
    argument = arguments[index]
    if argument in {"-c", "--config"}:
        return arguments[index + 1] if index + 1 < len(arguments) else None
    for prefix in ("-c=", "--config="):
        if argument.startswith(prefix):
            return argument.removeprefix(prefix)
    return None


def _protected_codex_config(config: str) -> bool:
    key = config.partition("=")[0].strip()
    return key in _CODEX_PROTECTED_CONFIG_PREFIXES or key.startswith(
        "model_providers."
    )


def _contains_codex_auth_command(arguments: tuple[str, ...]) -> bool:
    if len(arguments) > _MAXIMUM_CODEX_ARGUMENTS:
        return True
    for argument in arguments:
        if argument == "--":
            return False
        if argument in _CODEX_AUTH_COMMANDS:
            return True
    return False


def _foreground_process_group() -> int | None:
    if not os.isatty(0):
        return None
    try:
        return os.tcgetpgrp(0)
    except OSError:
        return None


def _set_foreground_process_group(
    process_group: int | None,
    original_group: int | None,
) -> None:
    if process_group is None or original_group is None:
        return
    previous = signal.getsignal(signal.SIGTTOU)
    try:
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, process_group)
    except OSError:
        pass
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _install_signal_forwarding(
    process_id: int,
) -> tuple[tuple[signal.Signals, SignalHandler], ...]:
    previous: list[tuple[signal.Signals, SignalHandler]] = []
    for member in _FORWARDED_SIGNALS:
        previous.append((member, signal.getsignal(member)))

        def forward(
            signal_number: int,
            _frame: FrameType | None,
            *,
            child: int = process_id,
        ) -> None:
            with suppress(ProcessLookupError):
                os.killpg(child, signal_number)

        signal.signal(member, forward)
    return tuple(previous)


def _restore_signal_handlers(
    previous: tuple[tuple[signal.Signals, SignalHandler], ...],
) -> None:
    for member, handler in previous:
        signal.signal(member, handler)


def _wait_for_child(
    process_id: int,
    original_group: int | None,
) -> int:
    while True:
        try:
            _waited, status = os.waitpid(process_id, os.WUNTRACED)
        except InterruptedError:
            continue
        if os.WIFSTOPPED(status):
            _set_foreground_process_group(original_group, original_group)
            os.kill(os.getpid(), os.WSTOPSIG(status))
            _set_foreground_process_group(process_id, original_group)
            os.killpg(process_id, signal.SIGCONT)
            continue
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
