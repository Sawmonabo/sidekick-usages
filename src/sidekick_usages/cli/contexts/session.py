"""Focused composition for provider-session shell enrollment."""

import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from rich.console import Console

from sidekick_usages.cli.contexts.use import compose_use_context
from sidekick_usages.cli.session.claude.commands import (
    ClaudeSavedAccountCommands,
)
from sidekick_usages.cli.session.claude.host import ClaudeCliSession
from sidekick_usages.cli.session.claude.runtime import ClaudeSessionRuntime
from sidekick_usages.cli.session.claude.terminal import (
    ClaudeTerminalApplication,
    ClaudeTerminalState,
)
from sidekick_usages.cli.session.codex import (
    CodexCliSession,
    CodexSessionRuntime,
)
from sidekick_usages.cli.session.launcher import ProviderSessionLauncher
from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
)
from sidekick_usages.cli.session.shell import (
    ShellEnrollment,
    ShellStartupResolver,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.platform.executable import (
    qualify_executable,
    resolve_executable_launcher,
)
from sidekick_usages.platform.host import detect_host_platform
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.credentials import native_claude_profile
from sidekick_usages.providers.claude.environment import (
    claude_private_profile_environment,
    claude_structured_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    discover_claude_executable,
)
from sidekick_usages.providers.claude.models import ClaudeNativeProfile
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredCapability,
    ClaudeStructuredError,
)
from sidekick_usages.providers.claude.structured.process import (
    ClaudeStructuredProcess,
    claude_arguments_mutate_auth,
    claude_structured_arguments_supported,
    qualify_claude_structured_capability,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.session.errors import CodexRelayError

_SIDEKICK_COMMAND = "sidekick-usages"
_PRIVATE_DIRECTORY_MODE = 0o700


class CodexSessionRunner(Protocol):
    """Run one qualified coordinated Codex CLI session."""

    def run(self, arguments: tuple[str, ...]) -> int:
        """Return the exact stock-TUI process status."""


class ClaudeSessionRunner(Protocol):
    """Run one release-qualified protected Claude session."""

    def run(self, arguments: tuple[str, ...]) -> int:
        """Return the official engine's natural exit status."""


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Provider-neutral explicit shell enrollment boundary."""

    shell: ShellEnrollment
    claude: ClaudeSessionRunner | None = None
    codex: CodexSessionRunner | None = None


class _CodexSessionRunner:
    """Lazily compose one qualified coordinated Codex session."""

    def __init__(
        self,
        paths: ApplicationPaths,
        environment: Mapping[str, str],
        sidekick_executable: ExecutableProvenance,
    ) -> None:
        self._paths = paths
        self._environment = dict(environment)
        self._sidekick_executable = sidekick_executable

    def run(self, arguments: tuple[str, ...]) -> int:
        """Compose and run the provider only for an explicit invocation."""
        try:
            return self._compose().run(arguments)
        except SessionLaunchError:
            raise
        except (UsageError, CodexRelayError) as error:
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTION_FAILED,
                str(error),
            ) from None

    def _compose(self) -> CodexCliSession:
        session_home = self._paths.codex_session_home
        launcher = ProviderSessionLauncher(
            self._environment,
            working_directory=Path.cwd(),
            sidekick_executable=self._sidekick_executable,
        )
        runtime = CodexSessionRuntime.create(
            discover_codex_executable(self._environment),
            session_home,
            self._paths.participant_sockets / f"codex-{uuid4()}.sock",
            self._paths.supervisor_socket,
            environment=self._environment,
        )
        return CodexCliSession(
            launcher,
            runtime,
            codex_home=session_home,
        )


class _ClaudeSessionRunner:
    """Lazily compose one exact-qualified coordinated Claude session."""

    def __init__(
        self,
        paths: ApplicationPaths,
        environment: Mapping[str, str],
        sidekick_executable: ExecutableProvenance,
    ) -> None:
        self._paths = paths
        self._environment = dict(environment)
        self._launcher = ProviderSessionLauncher(
            self._environment,
            working_directory=Path.cwd(),
            sidekick_executable=sidekick_executable,
        )

    def run(self, arguments: tuple[str, ...]) -> int:
        """Compose the protected host only for an explicit invocation."""
        try:
            if claude_arguments_mutate_auth(arguments):
                raise SessionLaunchError(
                    SessionLaunchFailure.UNSAFE_OVERRIDE,
                    "Claude authentication must use Sidekick selection.",
                )
            if claude_structured_arguments_supported(arguments):
                return self._compose(arguments).run(arguments)
            Console(stderr=True).print(
                "Sidekick: these arguments are not structured-host "
                "qualified; launching exact native Claude unmanaged.",
                markup=False,
            )
            planned = self._launcher.plan(ProviderId.CLAUDE, arguments)
            return self._launcher.run(planned)
        except SessionLaunchError:
            raise
        except (
            ClaudeManagedError,
            ClaudeProcessError,
            ClaudeStructuredError,
            UsageError,
        ) as error:
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTION_FAILED,
                str(error),
            ) from None

    def _compose(self, arguments: tuple[str, ...]) -> ClaudeCliSession:
        working_directory = Path.cwd()
        profile = self._native_profile()
        environment = claude_structured_environment(
            self._environment,
            profile,
        )
        capability = self._qualify_disposable()
        engine = ClaudeStructuredProcess.open(
            capability,
            environment,
            working_directory=working_directory,
            user_arguments=arguments,
        )
        try:
            runtime = ClaudeSessionRuntime.create(
                engine,
                self._paths.supervisor_socket,
            )
        except BaseException as error:
            failures: list[BaseException] = [error]
            try:
                engine.dispose_unenrolled()
            except BaseException as dispose_error:
                failures.append(dispose_error)
            if len(failures) > 1:
                raise BaseExceptionGroup(
                    "Claude composition and disposal both failed.",
                    failures,
                ) from None
            raise
        commands = ClaudeSavedAccountCommands(
            compose_use_context(paths=self._paths)
        )
        terminal_state = ClaudeTerminalState()
        return ClaudeCliSession(
            runtime,
            lambda: ClaudeTerminalApplication(commands, terminal_state),
        )

    def _native_profile(self) -> ClaudeNativeProfile:
        home = self._environment.get("HOME")
        if home is None or "\0" in home or not Path(home).is_absolute():
            raise SessionLaunchError(
                SessionLaunchFailure.INVALID_ARGUMENT,
                "The native Claude home must be an absolute path.",
            )
        return native_claude_profile(
            credential_home=Path(home) / ".claude",
            environment={},
        )

    def _qualify_disposable(
        self,
    ) -> ClaudeStructuredCapability:
        with tempfile.TemporaryDirectory(
            prefix="sidekick-claude-session-capability-"
        ) as raw_root:
            root = Path(raw_root)
            home = root / "home"
            config = root / "config"
            home.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            config.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            environment = claude_private_profile_environment(
                self._environment,
                process_home=home,
                config_directory=config,
            )
            executable = discover_claude_executable(
                environment,
                working_directory=home,
            )
            return qualify_claude_structured_capability(
                executable,
                detect_host_platform(environment=environment),
                environment,
                working_directory=home,
            )


def compose_session_context(
    *,
    paths: ApplicationPaths | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    effective_user_id: int | None = None,
    claude: ClaudeSessionRunner | None = None,
    codex: CodexSessionRunner | None = None,
) -> SessionContext:
    """Compose shell enrollment and lazy qualified provider owners."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_environment = os.environ if environment is None else environment
    uid = (
        getattr(os, "geteuid", lambda: 0)()
        if effective_user_id is None
        else effective_user_id
    )
    sidekick_executable = qualify_executable(
        resolve_executable_launcher(
            _SIDEKICK_COMMAND,
            resolved_environment,
        )
    )
    return SessionContext(
        ShellEnrollment(
            ShellStartupResolver(
                environment=resolved_environment,
                platform=sys.platform if platform is None else platform,
                posix_integration=resolved_paths.shell_integration,
                effective_user_id=uid,
            ),
            sidekick_executable,
        ),
        (
            _ClaudeSessionRunner(
                resolved_paths,
                resolved_environment,
                sidekick_executable,
            )
            if claude is None
            else claude
        ),
        (
            _CodexSessionRunner(
                resolved_paths,
                resolved_environment,
                sidekick_executable,
            )
            if codex is None
            else codex
        ),
    )
