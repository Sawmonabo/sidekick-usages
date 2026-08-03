"""Focused composition for provider-session shell enrollment."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

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
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.platform.executable import (
    qualify_executable,
    resolve_executable_launcher,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.session.errors import CodexRelayError

_SIDEKICK_COMMAND = "sidekick-usages"


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
        claude,
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
