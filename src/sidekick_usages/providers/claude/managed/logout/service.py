"""Official managed-Claude profile logout."""

import os
from collections.abc import Mapping

from sidekick_usages.providers.claude.auth.login.service import (
    verify_logged_out_claude_status,
)
from sidekick_usages.providers.claude.environment import (
    claude_private_profile_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_LOGOUT_OUTPUT_BYTES = 64 * 1024
_LOGOUT_TIMEOUT_SECONDS = 30.0
_PRIVATE_PROCESS_UMASK = 0o077


def logout_managed_claude_profile(
    capabilities: ClaudeCapabilities,
    source_environment: Mapping[str, str] | None = None,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> None:
    """Officially log out and verify one exact private Claude profile."""
    profile = capabilities.profile
    environment = claude_private_profile_environment(
        source_environment,
        process_home=profile.config_directory,
        config_directory=profile.config_directory,
    )
    verify_claude_executable(capabilities.executable)
    try:
        result = runner(
            (
                str(capabilities.executable.provenance.path),
                "auth",
                "logout",
            ),
            timeout_seconds=_LOGOUT_TIMEOUT_SECONDS,
            maximum_output_bytes=_LOGOUT_OUTPUT_BYTES,
            environment=environment,
            working_directory=profile.config_directory,
            umask=_PRIVATE_PROCESS_UMASK if os.name == "posix" else -1,
        )
    except ClaudeProcessError as error:
        try:
            verify_logged_out_claude_status(
                capabilities.executable,
                environment,
                profile.config_directory,
                runner=runner,
            )
        except ClaudeManagedError:
            raise error from None
        return
    finally:
        verify_claude_executable(capabilities.executable)
    del result
    verify_logged_out_claude_status(
        capabilities.executable,
        environment,
        profile.config_directory,
        runner=runner,
    )
