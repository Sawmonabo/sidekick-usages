"""Qualified managed Claude authority read-back and projection."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime

from sidekick_usages.core.accounts.models import (
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import AuthorityId, ProviderIdentity
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
)
from sidekick_usages.persistence.errors import (
    InvalidManagedArtifactError,
    PersistenceFilesystemError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
    ClaudeProtectedLogin,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
    protected_claude_login,
    read_protected_claude_authority,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.models import ClaudeManagedProfile
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeProfile,
)


class _ClaudeManagedCredentialFiles:
    """Read one exact private Claude profile through persistence."""

    def __init__(
        self,
        paths: ApplicationPaths,
        profiles: PrivateCredentialTree,
    ) -> None:
        if profiles.root != paths.private_claude_profiles:
            raise ValueError("Claude profile tree does not match its owner.")
        self._paths = paths
        self._profiles = profiles

    def read(self, profile: ClaudeProfile) -> bytes | None:
        """Return qualified bounded credentials or proven absence."""
        relative = self._relative(profile)
        try:
            snapshot = self._profiles.read_relative_authority_file(
                relative,
                CLAUDE_CREDENTIAL_FILE,
            )
        except InvalidManagedArtifactError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.MALFORMED
            ) from None
        except UnsafeManagedFileError, UnsupportedFilesystemError, ValueError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            ) from None
        except PersistenceFilesystemError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNREADABLE
            ) from None
        return None if snapshot is None else snapshot.data

    def present(self, profile: ClaudeProfile) -> bool:
        """Report the exact artifact without reading its contents."""
        relative = self._relative(profile)
        try:
            return self._profiles.relative_entry_present(
                relative,
                CLAUDE_CREDENTIAL_FILE,
            )
        except UnsafeManagedFileError, UnsupportedFilesystemError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            ) from None
        except PersistenceFilesystemError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNREADABLE
            ) from None

    def _relative(self, profile: ClaudeProfile) -> str:
        if not isinstance(profile, ClaudeManagedProfile):
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            )
        try:
            expected = managed_claude_config_dir(
                self._paths,
                profile.account_id,
            )
        except ValueError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            ) from None
        if profile.config_directory != expected:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            )
        return str(profile.account_id)


class ClaudeManagedAuthorityReader:
    """Compose qualified storage with strict provider read-back."""

    def __init__(
        self,
        paths: ApplicationPaths,
        profiles: PrivateCredentialTree,
    ) -> None:
        self._files = _ClaudeManagedCredentialFiles(paths, profiles)

    def read(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity | None = None,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> ClaudeAuthoritySnapshot:
        """Read one profile and bind its provider identity."""
        return read_protected_claude_authority(
            capabilities,
            self._files,
            reference_time,
            expected_identity=expected_identity,
            environment=environment,
            runner=runner,
        )

    def open_login(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> AbstractContextManager[ClaudeProtectedLogin]:
        """Open one short-lived protected refresh credential lease."""
        return protected_claude_login(
            capabilities,
            self._files,
            reference_time,
            expected_identity=expected_identity,
            environment=environment,
            runner=runner,
        )


def managed_login_authority(
    snapshot: ClaudeAuthoritySnapshot,
    authority_id: AuthorityId,
    verified_at: datetime,
) -> ClaudeManagedLoginAuthority:
    """Project verified credentials into secret-free saved metadata."""
    return ClaudeManagedLoginAuthority(
        authority_id=authority_id,
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        access_expires_at=snapshot.access_expires_at,
        refresh_expires_at=snapshot.refresh_expires_at,
        verified_at=verified_at,
        executable_version=snapshot.executable_version,
        health=snapshot.health,
        action=snapshot.action,
    )


def managed_authority_matches(
    account: SavedAccount,
    authority: ClaudeManagedLoginAuthority,
    snapshot: ClaudeAuthoritySnapshot,
) -> bool:
    """Match saved metadata to one exact protected Claude generation."""
    return (
        account.plan == snapshot.plan
        and authority.provider_identity == snapshot.provider_identity
        and authority.generation == snapshot.generation
        and authority.access_expires_at == snapshot.access_expires_at
        and authority.refresh_expires_at == snapshot.refresh_expires_at
        and authority.executable_version == snapshot.executable_version
    )
