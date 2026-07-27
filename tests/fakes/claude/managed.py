"""Synthetic managed Claude profiles and process behavior."""

import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.auth.login.models import ClaudeAuthStatus
from sidekick_usages.providers.claude.auth.login.service import (
    claude_status_association_key,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.executable import (
    MINIMUM_CLAUDE_VERSION,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeExecutable,
    ClaudeManagedProfile,
    ClaudeNativeProfile,
)
from sidekick_usages.providers.claude.types import (
    ClaudeProcessFailure,
    ClaudeProfile,
)

type ClaudeCommandScript = Callable[
    [
        tuple[str, ...],
        dict[str, str] | None,
        Path | None,
    ],
    ClaudeCommandResult,
]
type ClaudeProfileResponses = Mapping[
    tuple[Path, tuple[str, ...]],
    ClaudeCommandResult,
]

CLAUDE_LOGIN_HELP_OUTPUT = (
    b"Usage: claude auth login "
    b"[--claudeai] [--console] [--email <email>] [--sso]\n"
)
CLAUDE_LOGGED_IN_STATUS = (
    b'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty",'
    b'"email":"external@example.test","orgId":"provider-organization-external",'
    b'"orgName":"External Organization","subscriptionType":"team"}\n'
)
CLAUDE_LOGGED_OUT_STATUS = (
    b'{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}\n'
)
CLAUDE_VERSION_OUTPUT = b"2.1.220 (Claude Code)\n"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def use_synthetic_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the Python executable as the synthetic Claude CLI."""
    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )


class ClaudeRunner:
    """Record exact Claude commands and return one synthetic script."""

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], ClaudeCommandResult] | None = None,
        *,
        profile_responses: ClaudeProfileResponses | None = None,
        script: ClaudeCommandScript | None = None,
    ) -> None:
        if (responses is None) == (script is None):
            raise ValueError("Claude runner requires one response source.")
        self._responses = responses
        self._profile_responses = dict(profile_responses or {})
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
        cancelled: Callable[[], bool] | None = None,
    ) -> ClaudeCommandResult:
        if cancelled is not None and cancelled():
            raise ClaudeProcessError(ClaudeProcessFailure.CANCELLED)
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
        profile = self._profile_directory(captured_environment)
        if profile is not None:
            response = self._profile_responses.get((profile, arguments))
            if response is not None:
                return response
        if self._responses is None:
            raise AssertionError("Claude responses are unavailable.")
        try:
            return self._responses[arguments]
        except KeyError:
            raise AssertionError(
                f"Unexpected Claude command: {arguments!r}"
            ) from None

    @staticmethod
    def _profile_directory(
        environment: Mapping[str, str] | None,
    ) -> Path | None:
        if environment is None:
            return None
        configured = environment.get("CLAUDE_CONFIG_DIR")
        if configured is not None:
            return Path(configured)
        home = environment.get("HOME")
        return None if home is None else Path(home) / ".claude"


class ClaudeManagedLoginScript:
    """Write scripted official-login results into exact private profiles."""

    def __init__(
        self,
        profiles: PrivateCredentialTree,
        refresh_payloads: Mapping[Path, tuple[bytes | None, ...]],
        *,
        interactive_payloads: Mapping[Path, bytes] | None = None,
        profile_statuses: Mapping[Path, bytes] | None = None,
        refresh_statuses: Mapping[Path, tuple[bytes, ...]] | None = None,
    ) -> None:
        self._profiles = profiles
        self._refresh_payloads = {
            profile: list(payloads)
            for profile, payloads in refresh_payloads.items()
        }
        self._interactive_payloads = dict(interactive_payloads or {})
        self._profile_statuses = dict(profile_statuses or {})
        self._refresh_statuses = {
            profile: list(statuses)
            for profile, statuses in (refresh_statuses or {}).items()
        }
        self.login_profiles: list[Path] = []
        self.interactive_profiles: list[Path] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        del working_directory
        if arguments == ("--version",):
            return ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT)
        if arguments == ("auth", "login", "--help"):
            return ClaudeCommandResult(0, CLAUDE_LOGIN_HELP_OUTPUT)
        if arguments == ("auth", "status"):
            config_directory = self._config_directory(environment)
            configured = self._profile_statuses.get(config_directory)
            if configured is not None:
                return ClaudeCommandResult(0, configured)
            credential_file = config_directory / CLAUDE_CREDENTIAL_FILE
            return (
                ClaudeCommandResult(0, CLAUDE_LOGGED_IN_STATUS)
                if credential_file.is_file()
                else ClaudeCommandResult(1, CLAUDE_LOGGED_OUT_STATUS)
            )
        if arguments != ("auth", "login", "--claudeai"):
            raise AssertionError(f"Unexpected Claude command: {arguments!r}")
        config_directory = self._config_directory(environment)
        try:
            payload = self._refresh_payloads[config_directory].pop(0)
        except KeyError, IndexError:
            raise AssertionError(
                "Official login targeted an unexpected profile."
            ) from None
        self.login_profiles.append(config_directory)
        if payload is None:
            return ClaudeCommandResult(1, b"synthetic login rejected")
        self._write_credentials(config_directory, payload)
        statuses = self._refresh_statuses.get(config_directory)
        if statuses:
            self._profile_statuses[config_directory] = statuses.pop(0)
        return ClaudeCommandResult(0, b"synthetic official login complete")

    def interactive(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
    ) -> int:
        """Write one provider-controlled interactive login result."""
        del timeout_seconds, working_directory, umask
        if argv[1:] != ("auth", "login", "--claudeai"):
            raise AssertionError(
                f"Unexpected interactive Claude command: {argv[1:]!r}"
            )
        config_directory = self._config_directory(environment)
        try:
            payload = self._interactive_payloads[config_directory]
        except KeyError:
            raise AssertionError(
                "Interactive login targeted an unexpected profile."
            ) from None
        self._write_credentials(config_directory, payload)
        self.interactive_profiles.append(config_directory)
        return 0

    def set_status(
        self,
        config_directory: Path,
        payload: bytes,
    ) -> None:
        """Set explicit provider profile state after an external login."""
        self._profile_statuses[config_directory] = payload

    def _write_credentials(
        self,
        config_directory: Path,
        payload: bytes,
    ) -> None:
        if config_directory.is_relative_to(self._profiles.root):
            self._profiles.write_owned_file(
                config_directory,
                CLAUDE_CREDENTIAL_FILE,
                payload,
            )
            return
        credential_file = config_directory / CLAUDE_CREDENTIAL_FILE
        credential_file.write_bytes(payload)
        os.chmod(credential_file, _PRIVATE_FILE_MODE)

    @staticmethod
    def _config_directory(
        environment: Mapping[str, str] | None,
    ) -> Path:
        if environment is None:
            raise AssertionError("Claude process environment was omitted.")
        configured = environment.get("CLAUDE_CONFIG_DIR")
        if configured is not None:
            return Path(configured)
        return Path(environment["HOME"]) / ".claude"


def credential_payload(
    account_id: str | None,
    organization_id: str | None,
    *,
    token_suffix: str,
    access_expires_at: datetime,
    refresh_expires_at: datetime | None = None,
    scopes: tuple[str, ...] = ("user:profile", "user:inference"),
) -> bytes:
    """Encode one complete synthetic Claude credential envelope."""
    if (account_id is None) != (organization_id is None):
        raise ValueError("Synthetic Claude identity must be complete.")
    oauth: dict[str, object] = {
        "accessToken": f"sk-ant-oat01-{token_suffix}",
        "refreshToken": f"refresh-{token_suffix}",
        "expiresAt": int(access_expires_at.timestamp() * 1000),
        "subscriptionType": "pro",
        "scopes": list(scopes),
    }
    if account_id is not None and organization_id is not None:
        oauth["tokenAccount"] = {
            "accountUuid": account_id,
            "organizationUuid": organization_id,
        }
    if refresh_expires_at is not None:
        oauth["refreshTokenExpiresAt"] = int(
            refresh_expires_at.timestamp() * 1000
        )
    return json.dumps(
        {
            "claudeAiOauth": oauth,
        }
    ).encode()


def claude_auth_status_payload(
    email: str,
    organization_id: str,
    *,
    subscription_type: str = "pro",
) -> bytes:
    """Encode one exact synthetic official Claude status."""
    return json.dumps(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "email": email,
            "orgId": organization_id,
            "orgName": "Synthetic Organization",
            "subscriptionType": subscription_type,
        }
    ).encode()


def claude_auth_status_result(
    email: str,
    organization_id: str,
) -> ClaudeCommandResult:
    """Return one successful exact-profile official status result."""
    return ClaudeCommandResult(
        0,
        claude_auth_status_payload(email, organization_id),
    )


def claude_profile_status_responses(
    profiles: Mapping[Path, str],
) -> dict[tuple[Path, tuple[str, ...]], ClaudeCommandResult]:
    """Return explicit auth-status responses for exact profiles."""
    return {
        (profile, ("auth", "status")): claude_auth_status_result(
            f"{name}@example.test",
            f"provider-organization-{name}",
        )
        for profile, name in profiles.items()
    }


def claude_profile_status(
    name: str,
) -> tuple[bytes, ProviderIdentity]:
    """Return explicit status and association for one named profile."""
    email = f"{name}@example.test"
    organization_id = f"provider-organization-{name}"
    return (
        claude_auth_status_payload(email, organization_id),
        claude_status_identity(email, organization_id),
    )


def claude_status_identity(
    email: str,
    organization_id: str,
) -> ProviderIdentity:
    """Return the production association key for synthetic status."""
    identity = claude_status_association_key(
        ClaudeAuthStatus(
            return_code=0,
            logged_in=True,
            auth_method="claude.ai",
            api_provider="firstParty",
            email=email,
            organization_id=organization_id,
        )
    )
    if identity is None:
        raise AssertionError("Synthetic Claude status must be complete.")
    return identity


def claude_capabilities(
    profile: ClaudeProfile,
    platform: ClaudeManagedPlatform,
) -> ClaudeCapabilities:
    """Return capability-matched evidence for one synthetic profile."""
    executable_path = Path(sys.executable).resolve()
    return ClaudeCapabilities(
        ClaudeExecutable(
            executable_path,
            ExecutableProvenance.from_stat(
                executable_path,
                executable_path.stat(),
            ),
            MINIMUM_CLAUDE_VERSION,
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


def native_profile(root: Path) -> ClaudeNativeProfile:
    """Create one secure synthetic native-default Claude profile."""
    root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    profile = ClaudeNativeProfile(root / ".claude")
    profile.config_directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    return profile


def profile_tree(paths: ApplicationPaths) -> PrivateCredentialTree:
    """Return the exact managed Claude private tree."""
    return PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
