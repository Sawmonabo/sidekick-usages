"""Stable managed Claude profile capability composition."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from threading import Lock

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityResult,
)
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
from sidekick_usages.providers.claude.credentials import native_claude_profile
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
    claude_private_profile_environment,
)
from sidekick_usages.providers.claude.managed.capabilities import (
    managed_claude_platform,
    probe_claude_runtime_capabilities,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    discover_claude_executable,
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.models import (
    ClaudeCapabilities,
    ClaudeRuntimeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeManagedProfile,
    ClaudeNativeProfile,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_PRIVATE_DIRECTORY_MODE = 0o700


class ClaudeProfileCapabilityFactory:
    """Bind one invocation's capability proof to qualified profiles."""

    def __init__(
        self,
        paths: ApplicationPaths,
        profiles: PrivateCredentialTree,
        *,
        environment: Mapping[str, str] | None = None,
        host: HostPlatform | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> None:
        _require_profile_tree(paths, profiles)
        source = os.environ if environment is None else environment
        self._paths = paths
        self._profiles = profiles
        self._environment = dict(source)
        self._platform = managed_claude_platform(
            detect_host_platform(environment=source) if host is None else host
        )
        self._runner = runner
        self._result: ProviderCapabilityResult | None = None
        self._proof_lock = Lock()

    def managed(self, account_id: SidekickAccountId) -> ClaudeCapabilities:
        """Qualify and bind one stable private account profile."""
        profile = _qualified_managed_profile(self._paths, account_id)
        runtime = self.runtime()
        verify_claude_executable(runtime.executable)
        try:
            self._profiles.ensure_owned_directory(profile.config_directory)
        except PersistenceFilesystemError, ValueError:
            raise ClaudeManagedError(
                ClaudeManagedFailure.PROFILE_UNSAFE
            ) from None
        return runtime.bind(profile)

    def native(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ClaudeCapabilities:
        """Bind the proof to the native default Claude profile."""
        profile = _native_profile(environment)
        runtime = self.runtime()
        verify_claude_executable(runtime.executable)
        return runtime.bind(profile)

    def runtime(self) -> ClaudeRuntimeCapabilities:
        """Return or raise the authoritative runtime capability result."""
        result = self.result()
        capabilities = result.capabilities
        if isinstance(capabilities, ClaudeRuntimeCapabilities):
            return capabilities
        failure = result.failure
        if isinstance(failure, ClaudeManagedFailure):
            raise ClaudeManagedError(failure)
        raise AssertionError("Claude capability result is inconsistent.")

    def result(self) -> ProviderCapabilityResult:
        """Return one cached thread-safe provider capability result."""
        with self._proof_lock:
            if self._result is None:
                self._result = probe_claude_runtime_result(
                    self._platform,
                    self._environment,
                    self._runner,
                )
            return self._result


def probe_claude_runtime_result(
    platform: ClaudeManagedPlatform,
    environment: Mapping[str, str],
    runner: ClaudeCommandRunner,
) -> ProviderCapabilityResult:
    """Prove Claude capabilities in an isolated credential-free profile."""
    executable: ClaudeExecutable | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="sidekick-claude-capability-"
        ) as raw_root:
            probe_root = Path(raw_root)
            probe_home = probe_root / "home"
            probe_config = probe_root / "config"
            probe_home.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            probe_config.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            probe_environment = claude_private_profile_environment(
                environment,
                process_home=probe_home,
                config_directory=probe_config,
            )
            executable = discover_claude_executable(
                probe_environment,
                working_directory=probe_home,
                runner=runner,
            )
            capabilities = probe_claude_runtime_capabilities(
                executable,
                platform,
                probe_environment,
                probe_home,
                runner=runner,
            )
    except ClaudeManagedError as error:
        return ProviderCapabilityResult.failed(
            ProviderId.CLAUDE,
            error.code,
            executable=executable,
        )
    except OSError, ValueError:
        return ProviderCapabilityResult.failed(
            ProviderId.CLAUDE,
            ClaudeManagedFailure.PROFILE_UNSAFE,
            executable=executable,
        )
    return ProviderCapabilityResult.succeeded(
        ProviderId.CLAUDE,
        executable,
        capabilities,
    )


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
    _qualified_managed_profile(paths, account_id)
    return ClaudeProfileCapabilityFactory(
        paths,
        profiles,
        environment=environment,
        host=host,
        runner=runner,
    ).managed(account_id)


def _native_profile(
    environment: Mapping[str, str] | None,
) -> ClaudeNativeProfile:
    """Resolve the unconfigured native profile from one explicit home."""
    source = os.environ if environment is None else environment
    if CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY in source:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE)
    home = source.get("HOME")
    try:
        profile = (
            native_claude_profile(environment={})
            if home is None
            else native_claude_profile(
                credential_home=_native_config_directory(home),
                environment={},
            )
        )
    except ValueError:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE) from None
    return profile


def _qualified_managed_profile(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> ClaudeManagedProfile:
    """Return one normalized stable managed profile without side effects."""
    try:
        return ClaudeManagedProfile(
            account_id,
            managed_claude_config_dir(paths, account_id),
        )
    except ValueError:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE) from None


def _require_profile_tree(
    paths: ApplicationPaths,
    profiles: PrivateCredentialTree,
) -> None:
    """Require the exact protected tree before any provider process starts."""
    if profiles.root != paths.private_claude_profiles:
        raise ClaudeManagedError(ClaudeManagedFailure.PROFILE_UNSAFE)


def _native_config_directory(home: str) -> Path:
    """Return the native config path for one explicit absolute home."""
    if not home:
        raise ValueError("Claude native profile path is unavailable.")
    home_path = Path(home)
    if not home_path.is_absolute() or ".." in home_path.parts:
        raise ValueError("Claude native profile path is unavailable.")
    return home_path / ".claude"
