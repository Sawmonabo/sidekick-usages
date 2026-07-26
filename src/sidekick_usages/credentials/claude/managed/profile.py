"""Stable managed Claude profile capability composition."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
)
from sidekick_usages.persistence.errors import PersistenceFilesystemError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.host import detect_host_platform
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.environment import (
    claude_private_profile_environment,
)
from sidekick_usages.providers.claude.managed.capabilities import (
    managed_claude_platform,
    probe_claude_capabilities,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    discover_claude_executable,
)
from sidekick_usages.providers.claude.managed.models import (
    ClaudeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)
from sidekick_usages.providers.claude.models import ClaudeManagedProfile
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_PRIVATE_DIRECTORY_MODE = 0o700


def prepare_claude_managed_profile(
    paths: ApplicationPaths,
    profiles: PrivateCredentialTree,
    account_id: SidekickAccountId,
    *,
    environment: Mapping[str, str] | None = None,
    host: HostPlatform | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeCapabilities:
    """Prove capabilities, then create one stable protected profile."""
    source = os.environ if environment is None else environment
    current_host = (
        detect_host_platform(environment=source) if host is None else host
    )
    platform = managed_claude_platform(current_host)
    if profiles.root != paths.private_claude_profiles:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE)
    try:
        config_directory = managed_claude_config_dir(paths, account_id)
        profile = ClaudeManagedProfile(account_id, config_directory)
    except ValueError:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE) from None
    with tempfile.TemporaryDirectory(
        prefix="sidekick-claude-capability-"
    ) as raw_root:
        probe_root = Path(raw_root)
        probe_home = probe_root / "home"
        probe_config = probe_root / "config"
        probe_home.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        probe_config.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        probe_environment = claude_private_profile_environment(
            source,
            process_home=probe_home,
            config_directory=probe_config,
        )
        executable = discover_claude_executable(
            probe_environment,
            working_directory=probe_home,
            runner=runner,
        )
        capabilities = probe_claude_capabilities(
            executable,
            profile,
            platform,
            probe_environment,
            probe_home,
            runner=runner,
        )
    try:
        profiles.ensure_owned_directory(config_directory)
    except PersistenceFilesystemError, ValueError:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE) from None
    return capabilities
