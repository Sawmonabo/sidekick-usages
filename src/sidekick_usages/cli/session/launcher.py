"""Exact provider executable launch planning."""

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
    SessionLaunchSpec,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
    verify_executable_launcher,
)
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

type SessionProcessRunner = Callable[
    [tuple[str, ...], dict[str, str], Path],
    int,
]
type ProviderLauncherResolver = Callable[[Mapping[str, str]], Path]

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


def _run_provider(
    command: tuple[str, ...],
    environment: dict[str, str],
    working_directory: Path,
) -> int:
    """Run in the current terminal and process group without a shell."""
    return subprocess.run(
        command,
        check=False,
        cwd=working_directory,
        env=environment,
    ).returncode


class ProviderSessionLauncher:
    """Validate and freeze one official provider process launch."""

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        working_directory: Path | None = None,
        process_runner: SessionProcessRunner = _run_provider,
        claude_resolver: ProviderLauncherResolver = resolve_claude_launcher,
        codex_resolver: ProviderLauncherResolver = resolve_codex_launcher,
    ) -> None:
        source = os.environ if environment is None else environment
        self._environment = dict(source)
        self._working_directory = (
            Path.cwd() if working_directory is None else working_directory
        )
        self._process_runner = process_runner
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
        if executable.path.name == "sidekick-usages":
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
        """Run one unchanged plan in the caller's process group and TTY."""
        try:
            verify_executable_launcher(spec.launcher, spec.executable)
        except ExecutableQualificationError as error:
            raise SessionLaunchError(
                SessionLaunchFailure.EXECUTABLE_CHANGED,
                "The provider executable changed after qualification.",
            ) from error
        return self._process_runner(
            spec.command,
            dict(spec.environment),
            spec.working_directory,
        )

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
        ) or (
            provider_arguments
            and provider_arguments[0] in _CODEX_AUTH_COMMANDS
        ):
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
