"""Synthetic managed Claude profiles and process behavior."""

import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.managed.executable import (
    SUPPORTED_CLAUDE_VERSION,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeExecutable,
    ClaudeManagedProfile,
)

type ClaudeCommandScript = Callable[
    [
        tuple[str, ...],
        dict[str, str] | None,
        Path | None,
    ],
    ClaudeCommandResult,
]


class ClaudeRunner:
    """Record exact Claude commands and return one synthetic script."""

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], ClaudeCommandResult] | None = None,
        *,
        script: ClaudeCommandScript | None = None,
    ) -> None:
        if (responses is None) == (script is None):
            raise ValueError("Claude runner requires one response source.")
        self._responses = responses
        self._script = script
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.environments: list[dict[str, str] | None] = []
        self.working_directories: list[Path | None] = []
        self.timeouts: list[float] = []
        self.output_limits: list[int] = []
        self.umasks: list[int] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
    ) -> ClaudeCommandResult:
        arguments = argv[1:]
        captured_environment = (
            None if environment is None else dict(environment)
        )
        self.calls.append((Path(argv[0]), arguments))
        self.environments.append(captured_environment)
        self.working_directories.append(working_directory)
        self.timeouts.append(timeout_seconds)
        self.output_limits.append(maximum_output_bytes)
        self.umasks.append(umask)
        if self._script is not None:
            return self._script(
                arguments,
                captured_environment,
                working_directory,
            )
        if self._responses is None:
            raise AssertionError("Claude responses are unavailable.")
        try:
            return self._responses[arguments]
        except KeyError:
            raise AssertionError(
                f"Unexpected Claude command: {arguments!r}"
            ) from None


def credential_payload(
    account_id: str,
    organization_id: str,
    *,
    token_suffix: str,
    access_expires_at: datetime,
    scopes: tuple[str, ...] = ("user:profile", "user:inference"),
) -> bytes:
    """Encode one complete synthetic Claude credential envelope."""
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": f"sk-ant-oat01-{token_suffix}",
                "refreshToken": f"refresh-{token_suffix}",
                "expiresAt": int(access_expires_at.timestamp() * 1000),
                "subscriptionType": "pro",
                "scopes": list(scopes),
                "tokenAccount": {
                    "accountUuid": account_id,
                    "organizationUuid": organization_id,
                },
            }
        }
    ).encode()


def managed_capabilities(
    profile: ClaudeManagedProfile,
    platform: ClaudeManagedPlatform,
) -> ClaudeCapabilities:
    """Return release-matched capabilities for one synthetic profile."""
    executable_path = Path(sys.executable).resolve()
    return ClaudeCapabilities(
        ClaudeExecutable(
            ExecutableProvenance.from_stat(
                executable_path,
                executable_path.stat(),
            ),
            SUPPORTED_CLAUDE_VERSION,
        ),
        profile,
        platform,
    )


def managed_profile(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> ClaudeManagedProfile:
    """Return the exact synthetic profile for one stable account."""
    return ClaudeManagedProfile(
        account_id,
        managed_claude_config_dir(paths, account_id),
    )


def profile_tree(paths: ApplicationPaths) -> PrivateCredentialTree:
    """Return the exact managed Claude private tree."""
    return PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
