"""Focused composition for provider-session shell enrollment."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from sidekick_usages.cli.session.shell import (
    ShellEnrollment,
    ShellStartupResolver,
)
from sidekick_usages.paths import ApplicationPaths, discover_application_paths


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Provider-neutral explicit shell enrollment boundary."""

    shell: ShellEnrollment


def compose_session_context(
    *,
    paths: ApplicationPaths | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    effective_user_id: int | None = None,
) -> SessionContext:
    """Compose shell enrollment without provider credentials or daemons."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_environment = os.environ if environment is None else environment
    uid = (
        getattr(os, "geteuid", lambda: 0)()
        if effective_user_id is None
        else effective_user_id
    )
    return SessionContext(
        ShellEnrollment(
            ShellStartupResolver(
                environment=resolved_environment,
                platform=sys.platform if platform is None else platform,
                posix_integration=resolved_paths.shell_integration,
                effective_user_id=uid,
            )
        )
    )
