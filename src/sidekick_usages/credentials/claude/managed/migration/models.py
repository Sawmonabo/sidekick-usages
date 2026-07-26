"""Runtime dependencies for managed Claude migration."""

from collections.abc import Mapping
from dataclasses import dataclass

from sidekick_usages.core.accounts.identifiers import new_authority_id
from sidekick_usages.core.accounts.types import AuthorityIdFactory
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
    run_interactive_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeInteractiveCommandRunner,
)


@dataclass(frozen=True, slots=True)
class ClaudeMigrationRuntime:
    """Injectable host and process boundaries for one migration service."""

    environment: Mapping[str, str] | None = None
    host: HostPlatform | None = None
    runner: ClaudeCommandRunner = run_bounded_claude_command
    interactive_runner: ClaudeInteractiveCommandRunner = (
        run_interactive_claude_command
    )
    authority_id_factory: AuthorityIdFactory = new_authority_id
